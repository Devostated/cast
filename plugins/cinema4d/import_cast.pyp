import c4d
import os
import math
import mxutils
import sys

from c4d import plugins, Vector, Vector4d, CPolygon, gui, BaseObject

PLUGIN_RES_DIR = os.path.join(os.path.dirname(__file__), "res")

mxutils.ImportSymbols(PLUGIN_RES_DIR)

with mxutils.LocalImportPath(PLUGIN_RES_DIR):
    from cast import Cast, CastColor, Model, Animation, Instance, Metadata, File, Color

version = "1.78"
__pluginname__ = f"Cast {version} (*.cast)"


class CastLoader(plugins.SceneLoaderData):
    def Init(self, node, isCloneInit):
        data = node.GetDataInstance()
        data.SetBool(CAST_IMPORT_BIND_SKIN, True)
        data.SetBool(CAST_IMPORT_IK_HANDLES, True)
        data.SetBool(CAST_IMPORT_CONSTRAINTS, True)
        return True

    def Identify(self, node, name, probe, size):
        if "cast" in name[-4:]:
            return True
        return False

    def Load(self, node, name, doc, filterflags, error, bt):
        importCast(doc, node, name)
        c4d.EventAdd()
        return c4d.FILEERROR_NONE


def importMetadata(doc, meta):
    doc[c4d.DOCUMENT_INFO_AUTHOR] = meta.Author()
    doc[c4d.DOCUMENT_INFO_PRGCREATOR_NAME] = meta.Software()


def importCast(doc, node, path):
    cast = Cast.load(path)

    instances = []
    meta = None

    for root in cast.Roots():
        for child in root.ChildrenOfType(Model):
            importModelNode(doc, node, child, path)
        for child in root.ChildrenOfType(Animation):
            importAnimationNode(doc, child, path)
        for child in root.ChildrenOfType(Instance):
            instances.append(child)

        # Grab the first defined meta node, if there is one.
        meta = meta or root.ChildOfType(Metadata)

    if len(instances) > 0:
        importInstanceNodes(doc, node, instances, path)

    if meta:
        importMetadata(doc, meta)


def utilityBuildPath(root, asset):
    if os.path.isabs(asset):
        return asset

    root = os.path.dirname(root)
    return os.path.join(root, asset)


def utilityQuaternionToEuler(tempQuat):
    quaternion = c4d.Quaternion()
    # Normalize the quaternion to avoid acos domain issues without clamping w.
    tx = tempQuat[0]
    ty = tempQuat[1]
    tz = tempQuat[2]
    tw = tempQuat[3]

    norm = math.sqrt(tx * tx + ty * ty + tz * tz + tw * tw)
    if norm <= 1e-9:
        quaternion.SetAxis(Vector(0, 0, 0), 0)
        return c4d.utils.MatrixToHPB(quaternion.GetMatrix(), c4d.ROTATIONORDER_HPB)

    x = tx / norm
    y = ty / norm
    z = tz / norm
    w = tw / norm

    ww = 2 * math.acos(w)

    val = 1.0 - w * w
    s = math.sqrt(val) if val > 0.0 else 0.0

    if s <= 1e-9:
        ax = ay = az = 0
    else:
        ax = x / s
        ay = y / s
        az = z / s

    quaternion.SetAxis(Vector(ax, ay, -az), ww)
    return c4d.utils.MatrixToHPB(quaternion.GetMatrix(), c4d.ROTATIONORDER_HPB)


def utilityCreateDefaultMaterial(path, material):
    # We don't care much about this material since 99% of users will use the renderers material system
    # We just create it basic in a most convinient way to let the user convert it to their prefered renderer.

    mat = c4d.Material()
    mat.SetName(material.Name())

    mat[c4d.MATERIAL_USE_COLOR] = True

    reflLayer = mat.GetReflectionLayerIndex(0)

    switcher = {
        "albedo": c4d.MATERIAL_COLOR_SHADER,
        "diffuse": c4d.MATERIAL_COLOR_SHADER,
        "specular": reflLayer.GetDataID() + c4d.REFLECTION_LAYER_COLOR_TEXTURE,
        "normal": c4d.MATERIAL_NORMAL_SHADER,
        "metal": reflLayer.GetDataID() + c4d.REFLECTION_LAYER_TRANS_TEXTURE,
        "roughness": reflLayer.GetDataID() + c4d.REFLECTION_LAYER_MAIN_SHADER_ROUGHNESS,
        "gloss": reflLayer.GetDataID() + c4d.REFLECTION_LAYER_MAIN_SHADER_ROUGHNESS,
        "emissive": c4d.MATERIAL_LUMINANCE_SHADER,
    }

    # Loop and connect the slots
    slots = material.Slots()
    for slot in slots:
        connection = slots[slot]
        if not connection.__class__ is File:
            continue
        if not slot in switcher:
            continue

        if connection.__class__ is File:
            shader = c4d.BaseList2D(c4d.Xbitmap)
            shader[c4d.BITMAPSHADER_FILENAME] = utilityBuildPath(path, connection.Path())
            shader.SetName(slot)

            if slot in ["metal", "gloss", "roughness", "normal"]:
                shader[c4d.BITMAPSHADER_COLORPROFILE] = c4d.BITMAPSHADER_COLORPROFILE_LINEAR

            mat[switcher[slot]] = shader
            mat.InsertShader(shader)
        elif connection.__class__ is Color:
            # Handle color conversion if necessary, Cinema 4D color input is sRGB.
            # Not necessary but might work as a guide the users own materials
            if connection.ColorSpace() == "linear":
                rgba = CastColor.toSRGBFromLinear(connection.Rgba())
            else:
                rgba = connection.Rgba()
            mat[c4d.MATERIAL_COLOR_COLOR] = Vector(rgba[0], rgba[1], rgba[2])
        else:
            continue
        if slot == "normal":
            mat[c4d.MATERIAL_USE_NORMAL] = True
        elif slot in ["gloss", "roughness"]:
            # Using it to add a roughness texture, so the user convert the material to the prefered render engine
            mat[reflLayer.GetDataID() + c4d.REFLECTION_LAYER_MAIN_DISTRIBUTION] = GGX 
        elif slot == "emissive":
            mat[c4d.MATERIAL_USE_LUMINANCE] = True

    mat[c4d.REFLECTION_LAYER_IMPORTED] = True

    return mat


def importMaterialNode(context, path, material):
    # We're checking if the material is already present in the project or in the import context
    doc = c4d.documents.GetActiveDocument()
    for mat in doc.GetMaterials() + context.GetMaterials():
        if mat.GetName() == material.Name():
            mat.Message(c4d.MSG_UPDATE)
            mat.Update(True, True)
            return mat

    mat = utilityCreateDefaultMaterial(path, material)

    # TODO: going through all the updates functions to know which are actually necessary
    mat.Message(c4d.MSG_UPDATE)
    mat.Update(True, True)
    context.InsertMaterial(mat)

    return mat


def importModelNode(doc, node, model, path):
    # Extract the name of this model from the path
    modelName = model.Name() or os.path.splitext(os.path.basename(path))[0]

    # Create a collection for our objects
    modelNull = BaseObject(c4d.Onull)
    modelNull.SetName(modelName)
    modelNull[c4d.ID_BASELIST_ICON_COLORIZE_MODE] = c4d.ID_BASELIST_ICON_COLORIZE_MODE_CUSTOM
    modelNull[c4d.ID_BASELIST_ICON_COLOR] = Vector(0.816, 0.357, 0.259)

    doc.InsertObject(modelNull)
    modelNull.InsertTag(c4d.BaseTag(TAG_PLUGIN_ID))
    # Import skeleton for binds, materials for meshes
    bones = importSkeletonNode(modelNull, model.Skeleton())
    materialArray = {x.Name(): importMaterialNode(doc, path, x)
                     for x in model.Materials()}

    meshes = model.Meshes()
    meshHandles = {}

    for mesh in meshes:
        newMesh = BaseObject(c4d.Opolygon)
        newMesh.SetName(mesh.Name() or "CastMesh")

        # Store for later creating blend shapes if necessary.
        meshHandles[mesh.Hash()] = newMesh

        vertexPositions = mesh.VertexPositionBuffer()
        vertexCount = int(len(vertexPositions) / 3)

        faces = mesh.FaceBuffer()
        faceIndicesCount = len(faces)
        facesCount = int(faceIndicesCount / 3)

        newMesh.ResizeObject(vertexCount, facesCount)

        for i in range(0, len(vertexPositions), 3):
            newMesh.SetPoint(
                int(i / 3), Vector(vertexPositions[i], vertexPositions[i + 1], -vertexPositions[i + 2]))

        # Remap face indices to match c4d's winding order
        faces = [face for x in range(0, faceIndicesCount, 3)
                 for face in (faces[x + 2], faces[x + 1], faces[x + 0])]

        for i in range(0, faceIndicesCount, 3):
            newMesh.SetPolygon(
                int(i / 3), CPolygon(faces[i], faces[i + 1], faces[i + 2]))

        meshMaterial = mesh.Material()

        for i in range(mesh.UVLayerCount()):
            uvBuffer = mesh.VertexUVLayerBuffer(i)
            uvTag = c4d.UVWTag(facesCount)

            for j in range(0, faceIndicesCount, 3):
                uvTag.SetSlow(int(j / 3),
                              Vector(uvBuffer[faces[j] * 2],
                                     uvBuffer[(faces[j] * 2) + 1], 0),
                              Vector(uvBuffer[faces[j + 1] * 2],
                                     uvBuffer[(faces[j + 1] * 2) + 1], 0),
                              Vector(uvBuffer[faces[j + 2] * 2],
                                     uvBuffer[(faces[j + 2] * 2) + 1], 0),
                              Vector(0, 0, 0))

            newMesh.InsertTag(uvTag)

            if meshMaterial is not None and i < 1:
                material_tag = newMesh.MakeTag(c4d.Ttexture)
                material_tag[c4d.TEXTURETAG_MATERIAL] = materialArray[meshMaterial.Name()]
                material_tag[c4d.TEXTURETAG_PROJECTION] = c4d.TEXTURETAG_PROJECTION_UVW

        for i in range(mesh.ColorLayerCount()):
            vertexColors = mesh.VertexColorLayerBuffer(i)
            vcTag = c4d.VertexColorTag(vertexCount)
            vcData = vcTag.GetDataAddressW()

            for v in range(vertexCount):
                color = CastColor.fromInteger(vertexColors[v])
                vcTag.SetPoint(vcData, None, None, v,
                               Vector4d(color[0], color[1], color[2], color[3]))

            newMesh.InsertTag(vcTag)

        vertexNormals = mesh.VertexNormalBuffer()
        if vertexNormals is not None:
            vnTag = c4d.NormalTag(facesCount)
            vnData = vnTag.GetDataAddressW()

            for i in range(0, faceIndicesCount, 3):
                vnTag.Set(vnData, int(i / 3), 
                         {"a": Vector(vertexNormals[faces[i] * 3],
                                       vertexNormals[(faces[i] * 3) + 1],
                                       -vertexNormals[(faces[i] * 3) + 2]),
                          "b": Vector(vertexNormals[faces[i + 1] * 3],
                                        vertexNormals[(faces[i + 1] * 3) + 1],
                                        -vertexNormals[(faces[i + 1] * 3) + 2]),
                          "c": Vector(vertexNormals[faces[i + 2] * 3],
                                        vertexNormals[(faces[i + 2] * 3) + 1],
                                        -vertexNormals[(faces[i + 2] * 3) + 2]),
                          "d": Vector(0, 0, 0)})

            newMesh.InsertTag(vnTag)

        if bones is not None and node[CAST_IMPORT_BIND_SKIN]:
            skinObj = BaseObject(c4d.Oskin)

            skinningMethod = mesh.SkinningMethod()

            if skinningMethod == "linear":
                skinObj[c4d.ID_CA_SKIN_OBJECT_TYPE] = c4d.ID_CA_SKIN_OBJECT_TYPE_LINEAR
            elif skinningMethod == "quaternion":
                skinObj[c4d.ID_CA_SKIN_OBJECT_TYPE] = c4d.ID_CA_SKIN_OBJECT_TYPE_QUAT

            doc.InsertObject(skinObj, parent=newMesh)

            weightTag = c4d.modules.character.CAWeightTag()

            newMesh.InsertTag(weightTag)

            for bone in bones.values():
                weightTag.AddJoint(bone)

            maximumInfluence = mesh.MaximumWeightInfluence()
            # Use SetWeightMap for performance instead of many SetWeight calls.
            if maximumInfluence > 0:
                weightBoneBuffer = mesh.VertexWeightBoneBuffer()
                # For complex blends we also need the values buffer
                weightValueBuffer = mesh.VertexWeightValueBuffer() if maximumInfluence > 1 else None

                jointCount = weightTag.GetJointCount()
                # Initialize maps per joint
                maps = [[0.0] * vertexCount for _ in range(jointCount)]

                if maximumInfluence > 1:
                    for v in range(vertexCount):
                        base = v * maximumInfluence
                        for j in range(maximumInfluence):
                            idx = base + j
                            jointIndex = weightBoneBuffer[idx]
                            if jointIndex < 0 or jointIndex >= jointCount:
                                continue
                            maps[jointIndex][v] += weightValueBuffer[idx]
                else:
                    # Single influence per-vertex, assume full weight
                    for v in range(vertexCount):
                        jointIndex = weightBoneBuffer[v]
                        if jointIndex < 0 or jointIndex >= jointCount:
                            continue
                        maps[jointIndex][v] = 1.0

                # Apply maps using SetWeightMap
                for jidx in range(jointCount):
                    # Only set when there is at least one non-zero weight to avoid unnecessary calls
                    map_j = maps[jidx]
                    if any(w != 0.0 for w in map_j):
                        weightTag.SetWeightMap(jidx, map_j)

            weightTag.Message(c4d.MSG_UPDATE)

        doc.InsertObject(newMesh, parent=modelNull)
        newMesh.Message(c4d.MSG_UPDATE)

    if node[CAST_IMPORT_IK_HANDLES]:
        importSkeletonIKNode(model.Skeleton(), bones)

    if node[CAST_IMPORT_CONSTRAINTS]:
        importSkeletonConstraintNode(model.Skeleton(), bones)

    # TODO: Does this do anything?
    c4d.EventAdd()
    modelNull.Message(c4d.MSG_UPDATE)

    return modelNull


def importSkeletonConstraintNode(skeleton, bones):
    if skeleton is None:
        return

    for constraint in skeleton.Constraints():
        constraintBone = bones[constraint.ConstraintBone().Name()]
        targetBone = bones[constraint.TargetBone().Name()]

        type = constraint.ConstraintType()
        customOffset = constraint.CustomOffset()
        maintainOffset = constraint.MaintainOffset()
        weight = constraint.Weight()

        # C4D's constraint system is a bit worse than Blender's
        constraintTag = c4d.BaseTag(c4d.Tcaconstraint)
        constraintBone.InsertTag(constraintTag)
        constraintTag[c4d.ID_CA_CONSTRAINT_TAG_PSR] = True

        # Disabling all constraints, cause default is enabled
        constraintTag[CONSTRAIN_POS] = False
        constraintTag[c4d.ID_CA_CONSTRAINT_TAG_PSR_CONSTRAIN_P_X] = False
        constraintTag[c4d.ID_CA_CONSTRAINT_TAG_PSR_CONSTRAIN_P_Y] = False
        constraintTag[c4d.ID_CA_CONSTRAINT_TAG_PSR_CONSTRAIN_P_Z] = False

        constraintTag[CONSTRAIN_SCALE] = False
        constraintTag[c4d.ID_CA_CONSTRAINT_TAG_PSR_CONSTRAIN_S_X] = False
        constraintTag[c4d.ID_CA_CONSTRAINT_TAG_PSR_CONSTRAIN_S_Y] = False
        constraintTag[c4d.ID_CA_CONSTRAINT_TAG_PSR_CONSTRAIN_S_Z] = False

        constraintTag[CONSTRAIN_ROT] = False
        constraintTag[c4d.ID_CA_CONSTRAINT_TAG_PSR_CONSTRAIN_R_X] = False
        constraintTag[c4d.ID_CA_CONSTRAINT_TAG_PSR_CONSTRAIN_R_Y] = False
        constraintTag[c4d.ID_CA_CONSTRAINT_TAG_PSR_CONSTRAIN_R_Z] = False

        constraintTag[c4d.ID_CA_CONSTRAINT_TAG_PSR_MAINTAIN] = maintainOffset
        constraintTag[c4d.ID_CA_CONSTRAINT_TAG_PSR_TWEIGHT] = weight

        if type == "pt":
            constraintTag[CONSTRAIN_POS] = True
            constraintTag[CONSTRAINT_TARGET] = targetBone
            constraintTag[c4d.ID_CA_CONSTRAINT_TAG_LOCAL_P] = True

            if customOffset:
                constraintTag[c4d.ID_CA_CONSTRAINT_TAG_PSR_P_OFFSET] = Vector(customOffset)
            if not constraint.SkipX():
                constraintTag[c4d.ID_CA_CONSTRAINT_TAG_PSR_CONSTRAIN_P_X] = True
            if not constraint.SkipY():
                constraintTag[c4d.ID_CA_CONSTRAINT_TAG_PSR_CONSTRAIN_P_Y] = True
            if not constraint.SkipZ():
                constraintTag[c4d.ID_CA_CONSTRAINT_TAG_PSR_CONSTRAIN_P_Z] = True
        elif type == "sc":
            constraintTag[CONSTRAIN_SCALE] = True
            constraintTag[CONSTRAINT_TARGET] = targetBone
            constraintTag[c4d.ID_CA_CONSTRAINT_TAG_LOCAL_S] = True

            if customOffset:
                constraintTag[c4d.ID_CA_CONSTRAINT_TAG_PSR_S_OFFSET] = Vector(customOffset)
            if not constraint.SkipX():
                constraintTag[c4d.ID_CA_CONSTRAINT_TAG_PSR_CONSTRAIN_S_X] = True
            if not constraint.SkipY():
                constraintTag[c4d.ID_CA_CONSTRAINT_TAG_PSR_CONSTRAIN_S_Y] = True
            if not constraint.SkipZ():
                constraintTag[c4d.ID_CA_CONSTRAINT_TAG_PSR_CONSTRAIN_S_Z] = True
        elif type == "or":
            constraintTag[CONSTRAIN_ROT] = True
            constraintTag[CONSTRAINT_TARGET] = targetBone
            constraintTag[c4d.ID_CA_CONSTRAINT_TAG_LOCAL_R] = True

            if customOffset:
                constraintTag[c4d.ID_CA_CONSTRAINT_TAG_PSR_R_OFFSET] = Vector(customOffset)
            if not constraint.SkipX():
                constraintTag[c4d.ID_CA_CONSTRAINT_TAG_PSR_CONSTRAIN_R_X] = True
            if not constraint.SkipY():
                constraintTag[c4d.ID_CA_CONSTRAINT_TAG_PSR_CONSTRAIN_R_Y] = True
            if not constraint.SkipZ():
                constraintTag[c4d.ID_CA_CONSTRAINT_TAG_PSR_CONSTRAIN_R_Z] = True
        else:
            continue

        if constraint.Name() is not None:
            constraintTag[c4d.ID_BASELIST_NAME] = constraint.Name()


def importSkeletonIKNode(skeleton, bones):
    if skeleton is None or not skeleton.IKHandles():
        return

    for handle in skeleton.IKHandles():
        startBone = bones[handle.StartBone().Name()]
        endBone = bones[handle.EndBone().Name()]
        targetBone = bones[handle.TargetBone().Name()]

        constraintTag = c4d.BaseTag(c4d.Tcaconstraint)
        endBone.InsertTag(constraintTag)
        constraintTag[c4d.ID_CA_CONSTRAINT_TAG_PSR] = True
        constraintTag[CONSTRAINT_TARGET] = targetBone

        ikTag = c4d.BaseTag(IK_TAG)
        ikTag[c4d.ID_CA_IK_TAG_SOLVER] = 2
        ikTag[c4d.ID_CA_IK_TAG_TIP] = endBone
        ikTag[c4d.ID_CA_IK_TAG_TARGET] = targetBone
        startBone.InsertTag(ikTag)

        poleVectorBone = handle.PoleVectorBone()
        if poleVectorBone is not None:
            # Changing the IK solver from 3D to 2D to activate the pole input
            ikTag[c4d.ID_CA_IK_TAG_SOLVER] = 1
            ikTag[c4d.ID_CA_IK_TAG_POLE] = bones[poleVectorBone.Name()]


        poleBone = handle.PoleBone()
        if poleBone is not None:
            poleBone = bones[poleBone.Name()]
            xpressoTag = c4d.BaseTag(c4d.Texpresso)
            startBone.InsertTag(xpressoTag)
            
            gvNodeMaster = xpressoTag.GetNodeMaster()
            poleNode = gvNodeMaster.CreateNode(parent = gvNodeMaster.GetRoot(),
                                    id = c4d.ID_OPERATOR_OBJECT,
                                    x = 100,
                                    y = 200 )
            poleNode[c4d.GV_OBJECT_OBJECT_ID] = poleBone
            poleRotYPort = poleNode.AddPort(c4d.GV_PORT_OUTPUT, [c4d.ID_BASEOBJECT_REL_ROTATION, c4d.VECTOR_Y])
            
            twistNode = gvNodeMaster.CreateNode(parent = gvNodeMaster.GetRoot(),
                                    id = c4d.ID_OPERATOR_OBJECT,
                                    x = 400,
                                    y = 200 )
            twistNode[c4d.GV_OBJECT_OBJECT_ID] = ikTag
            twistInPort = twistNode.AddPort(c4d.GV_PORT_INPUT, c4d.ID_CA_IK_TAG_POLE_TWIST)

            poleRotYPort.Connect(twistInPort)


def importSkeletonNode(modelNull, skeleton):
    if skeleton is None:
        return None

    bones = skeleton.Bones()
    handles = [None] * len(bones)
    boneNames = {}

    for i, bone in enumerate(bones):
        newBone = BaseObject(c4d.Ojoint)
        newBone.SetName(bone.Name())

        tX, tY, tZ = bone.LocalPosition()
        translation = Vector(tX, tY, -tZ)

        rotation = utilityQuaternionToEuler(bone.LocalRotation())

        scale = bone.Scale() or (1.0, 1.0, 1.0)
        scale = Vector(scale[0], scale[1], scale[2])

        newBone.SetAbsPos(translation)
        newBone.SetAbsRot(rotation)
        newBone.SetAbsScale(scale)

        handles[i] = newBone
        boneNames[bone.Name()] = newBone

    for i, bone in enumerate(bones):
        if bone.ParentIndex() > -1:
            handles[i].InsertUnder(handles[bone.ParentIndex()])
        else:
            handles[i].InsertUnder(modelNull)

    return boneNames


def utilityAddKeyframe(curve, time, value):
    key = curve.AddKey(time)
    key["key"].SetValue(curve, value)


def utilityGetTrack(targetObj, descid, mode):
    track = targetObj.FindCTrack(descid)
    if track:
        track.FlushData()
    else:
        track = c4d.CTrack(targetObj, descid)
        targetObj.InsertTrackSorted(track)
    return track


def utilityResolveCurveModeOverride(obj, mode, overrides, isTranslate=False, isRotate=False, isScale=False):
    if not overrides:
        return mode

    for parent in obj.GetUp():
        if parent.GetType() == c4d.Ojoint:
            for override in overrides:
                if isTranslate and not override.OverrideTranslationCurves():
                    continue
                elif isRotate and not override.OverrideRotationCurves():
                    continue
                elif isScale and not override.OverrideScaleCurves():
                    continue

                if parent.GetName() == override.NodeName():
                    return override.Mode()


def utilityRotCurveNode(doc, mode, targetObj, overrides, keyFrameBuffer, keyValueBuffer, fps):
    rotationMap = {
        "x": c4d.DescID(c4d.DescLevel(c4d.ID_BASEOBJECT_ROTATION, c4d.DTYPE_VECTOR, 0),
                          c4d.DescLevel(c4d.VECTOR_X, c4d.DTYPE_REAL, 0)),
        "y": c4d.DescID(c4d.DescLevel(c4d.ID_BASEOBJECT_ROTATION, c4d.DTYPE_VECTOR, 0),
                          c4d.DescLevel(c4d.VECTOR_Y, c4d.DTYPE_REAL, 0)),
        "z": c4d.DescID(c4d.DescLevel(c4d.ID_BASEOBJECT_ROTATION, c4d.DTYPE_VECTOR, 0),
                          c4d.DescLevel(c4d.VECTOR_Z, c4d.DTYPE_REAL, 0))
    }
    
    tracks = {axis: utilityGetTrack(targetObj, descid, mode) for axis, descid in rotationMap.items()}
    curves = {axis: track.GetCurve() for axis, track in tracks.items()}
    mode = utilityResolveCurveModeOverride(
        targetObj, mode, overrides, isRotate=True)

    for curve in curves.values():
        curve.FlushKeys()
    
    minTime = doc.GetMinTime()
    for i, frame in enumerate(keyFrameBuffer):
        key_time = minTime + c4d.BaseTime(frame, fps)
        quat = keyValueBuffer[i * 4:(i + 1) * 4]
        euler = utilityQuaternionToEuler(quat)
        if mode == "relative" or mode == "additive":
            euler += targetObj.GetRelRot()

        utilityAddKeyframe(curves["x"], key_time, euler.x)
        utilityAddKeyframe(curves["y"], key_time, euler.y)
        utilityAddKeyframe(curves["z"], key_time, euler.z)
    
    targetObj.FindBestEulerAngle(c4d.ID_BASEOBJECT_ROTATION, True, False)


def utilityCurveNodes(doc, mode, targetObj, overrides, keyFrameBuffer, keyValueBuffer, fps, prop):
    propDescMap = {
        "tx": c4d.DescID(c4d.DescLevel(c4d.ID_BASEOBJECT_POSITION, c4d.DTYPE_VECTOR, 0),
                           c4d.DescLevel(c4d.VECTOR_X, c4d.DTYPE_REAL, 0)),
        "ty": c4d.DescID(c4d.DescLevel(c4d.ID_BASEOBJECT_POSITION, c4d.DTYPE_VECTOR, 0),
                           c4d.DescLevel(c4d.VECTOR_Y, c4d.DTYPE_REAL, 0)),
        "tz": c4d.DescID(c4d.DescLevel(c4d.ID_BASEOBJECT_POSITION, c4d.DTYPE_VECTOR, 0),
                           c4d.DescLevel(c4d.VECTOR_Z, c4d.DTYPE_REAL, 0)),
        "sx": c4d.DescID(c4d.DescLevel(c4d.ID_BASEOBJECT_SCALE, c4d.DTYPE_VECTOR, 0),
                           c4d.DescLevel(c4d.VECTOR_X, c4d.DTYPE_REAL, 0)),
        "sy": c4d.DescID(c4d.DescLevel(c4d.ID_BASEOBJECT_SCALE, c4d.DTYPE_VECTOR, 0),
                           c4d.DescLevel(c4d.VECTOR_Y, c4d.DTYPE_REAL, 0)),
        "sz": c4d.DescID(c4d.DescLevel(c4d.ID_BASEOBJECT_SCALE, c4d.DTYPE_VECTOR, 0),
                           c4d.DescLevel(c4d.VECTOR_Z, c4d.DTYPE_REAL, 0))
    }

    propRelMap = {
        "tx": targetObj.GetRelPos().x,
        "ty": targetObj.GetRelPos().y,
        "tz": targetObj.GetRelPos().z,
        "sx": targetObj.GetRelScale().x,
        "sy": targetObj.GetRelScale().y,
        "sz": targetObj.GetRelScale().z,
    }
    
    descid = propDescMap.get(prop)
    relative = propRelMap.get(prop)
    if not descid:
        return None
    
    if prop in ["tx", "ty", "tz"]:
        mode = utilityResolveCurveModeOverride(
            targetObj, mode, overrides, isTranslate=True)
    elif prop in ["sx", "sy", "sz"]:
        mode = utilityResolveCurveModeOverride(
            targetObj, mode, overrides, isScale=True)
    
    track = utilityGetTrack(targetObj, descid, mode)
    curve = track.GetCurve()
    curve.FlushKeys()
    
    minTime = doc.GetMinTime()
    for frame, value in zip(keyFrameBuffer, keyValueBuffer):
        key_time = minTime + c4d.BaseTime(frame, fps)
        if prop == "tz":
            value = -value
        if mode == "relative" or mode == "additive":
            value += relative
        utilityAddKeyframe(curve, key_time, value)
    

def importAnimationNode(doc, node, animationNode):
    castImportPath = node[c4d.TCAST_IMPORT_PATH]
    castImportTime = node[c4d.TCAST_IMPORT_TIME]
    castImportReset = node[c4d.TCAST_IMPORT_RESET]
    tagObject = node.GetObject()


    animName = animationNode.Name() or os.path.splitext(os.path.basename(castImportPath))[0]

    joints = utilityGetJointsInHierarchy(tagObject)
    for joint in joints:
        joint[c4d.ID_BASEOBJECT_QUATERNION_ROTATION_INTERPOLATION] = 1

    takeData = doc.GetTakeData()
    mainTake = takeData.GetMainTake()
    if castImportReset:
        utilityResetBindPose(doc, node)
        actionTake = takeData.AddTake(name=animName, parent=mainTake, cloneFrom=None)
        actionTake.OverrideNode(takeData, node, False)
        actionTake.OverrideNode(takeData, tagObject, False)
        for joint in joints:
            actionTake.OverrideNode(takeData, joint, False)
    else:
        actionTake = takeData.GetCurrentTake()
    takeData.SetCurrentTake(actionTake)

    

    doc.SetFps(int(animationNode.Framerate()))
    fps = doc.GetFps()

    # We need to determine the proper time to import the curves, for example
    # the user may want to import at the current scene time, and that would require
    # fetching once here, then passing to the curve importer.
    wantedSmallestFrame = sys.maxsize
    wantedLargestFrame = 1

    if castImportTime:
        doc.SetMinTime(c4d.BaseTime(doc.GetTime().GetFrame(fps), fps))
    else:
        doc.SetMinTime(c4d.BaseTime(0, fps))
    minTime = doc.GetMinTime()






    curves = animationNode.Curves()
    curveModeOverrides = animationNode.CurveModeOverrides()

    poseBones = { bone.GetName().lower(): bone for bone in joints }
    curves = animationNode.Curves()
    for curve in curves:
        targetJoint = poseBones.get(curve.NodeName().lower())
        if targetJoint is not None:
            # importCastCurveAnimation(doc, curve, targetJoint)
            keyFrameBuffer = curve.KeyFrameBuffer()
            keyValueBuffer = curve.KeyValueBuffer()
            if not keyFrameBuffer or not keyValueBuffer:
                return
            property = curve.KeyPropertyName().lower()
            mode = curve.Mode()
            if property == "rq":
                utilityRotCurveNode(doc, mode, targetJoint, curveModeOverrides, keyFrameBuffer, keyValueBuffer, fps)
            else:
                utilityCurveNodes(doc, mode, targetJoint, curveModeOverrides, keyFrameBuffer, keyValueBuffer, fps, property)
                
            

    for x in animationNode.Notifications():
        (smallestFrame, largestFrame) = importNotificationTrackNode(doc, animName, x, fps, minTime)
        wantedSmallestFrame = min(smallestFrame, wantedSmallestFrame)
        wantedLargestFrame = max(largestFrame, wantedLargestFrame)




    # Update frame boundaries based on curve keyframes.
    for curve in curves:
        keyFrames = curve.KeyFrameBuffer()
        if keyFrames:
            smallest = min(keyFrames)
            largest = max(keyFrames)
            if smallest < wantedSmallestFrame:
                wantedSmallestFrame = smallest
            if largest > wantedLargestFrame:
                wantedLargestFrame = largest

    doc.SetMaxTime(minTime.__add__(c4d.BaseTime(wantedLargestFrame, fps)))


def utilityLayerManage(doc, name):
    layerRoot = doc.GetLayerObjectRoot()
    layer = layerRoot.GetDown()

    while layer:
        if layer.GetName() == name:
            return layer
        layer = layer.GetNext()

    newLayer = c4d.documents.LayerObject()
    newLayer.SetName(name)
    newLayer.InsertUnder(layerRoot)

    c4d.EventAdd()
    return newLayer


def importNotificationTrackNode(doc, animName, node, fps, frameStart):
    frameBuffer = node.KeyFrameBuffer()
    layer = utilityLayerManage(doc, animName)

    smallestFrame = sys.maxsize
    largestFrame = 0

    lastMarker = c4d.documents.GetFirstMarker(doc)
    while lastMarker and lastMarker.GetNext():
        lastMarker = lastMarker.GetNext()

    for frameOffset in frameBuffer:
        baseTime = c4d.BaseTime(frameOffset, fps) + frameStart

        marker = c4d.documents.AddMarker(doc, lastMarker, baseTime, node.Name())
        if marker:
            marker[c4d.ID_LAYER_LINK] = layer
            lastMarker = marker

            smallestFrame = min(smallestFrame, smallestFrame)
            largestFrame = max(largestFrame, largestFrame)

    c4d.EventAdd()
    return (smallestFrame, largestFrame)


def utilityGetJointsInHierarchy(obj, joints=None):
    if joints is None:
        joints = []

    if obj.GetType() == c4d.Ojoint:
        joints.append(obj)

    child = obj.GetDown()
    while child:
        utilityGetJointsInHierarchy(child, joints)
        child = child.GetNext()

    return joints


def utilityResetBindPose(doc, castTag):
    obj = castTag.GetObject()
    mesh = []

    child = obj.GetDown() if obj else None
    while child:
        if child.GetType() == c4d.Opolygon:
            mesh.append(child)
        child = child.GetNext()

    for m in mesh:
        weightTag = m.GetTag(c4d.Tweights)
        if weightTag:
            weightTag.ResetBindPose(doc, True)

    return True


def importInstanceNodes(doc, node, instanceNodes, path):
    rootPath = c4d.storage.LoadDialog(
        title='Select the root directory where instance scenes are located', flags=2)

    if rootPath is None:
        return gui.MessageDialog(text="Unable to import instances without a root directory!", type=c4d.GEMB_ICONSTOP)

    uniqueInstances = {}
    instanceImportError = False

    for instance in instanceNodes:
        refs = os.path.join(rootPath, instance.ReferenceFile().Path())

        if refs in uniqueInstances:
            uniqueInstances[refs].append(instance)
        else:
            uniqueInstances[refs] = [instance]

    name = os.path.splitext(os.path.basename(path))[0]

    # Create a collection for our objects
    rootNull = BaseObject(c4d.Onull)
    rootNull.SetName(name)
    rootNull[c4d.ID_BASELIST_ICON_COLORIZE_MODE] = c4d.ID_BASELIST_ICON_COLORIZE_MODE_CUSTOM
    rootNull[c4d.ID_BASELIST_ICON_COLOR] = Vector(0.816, 0.357, 0.259)

    doc.InsertObject(rootNull)

    instanceNull = BaseObject(c4d.Onull)
    instanceNull.SetName("%s_instances" % name)
    instanceNull.InsertUnder(rootNull)

    sceneNull = BaseObject(c4d.Onull)
    sceneNull.SetName("%s_scenes" % name)
    sceneNull.InsertUnder(rootNull)

    # Disable source models visibility
    sceneNull[c4d.ID_BASEOBJECT_VISIBILITY_EDITOR] = c4d.OBJECT_OFF
    sceneNull[c4d.ID_BASEOBJECT_VISIBILITY_RENDER] = c4d.OBJECT_OFF

    for instancePath, instances in uniqueInstances.items():
        instanceName = os.path.splitext(os.path.basename(instancePath))[0]

        try:
            cast = Cast.load(instancePath)
            for root in cast.Roots():
                for child in root.ChildrenOfType(Model):
                    modelNull = importModelNode(doc, node, child, instancePath)
            modelNull.InsertUnder(sceneNull)
        except:
            print("Failed to import instance: %s" % instancePath)
            instanceImportError = True
            continue

        for instance in instances:
            # Creates instance object
            newInstance = c4d.InstanceObject()

            newInstance.SetName(instance.Name() or instanceName)
            newInstance.InsertUnder(instanceNull)

            tX, tY, tZ = instance.Position()
            translation = Vector(tX, tY, -tZ)

            rotation = utilityQuaternionToEuler(instance.Rotation())

            scaleTuple = instance.Scale() or (1.0, 1.0, 1.0)
            scale = Vector(scaleTuple[0], scaleTuple[1], scaleTuple[2])

            newInstance.SetAbsPos(translation)
            newInstance.SetAbsRot(rotation)
            newInstance.SetAbsScale(scale)

            newInstance.SetReferenceObject(modelNull)

    if instanceImportError:
        gui.MessageDialog(text="Some instances failed to import.\nCheck the console for more details. ", type=c4d.GEMB_ICONEXCLAMATION)


class CastTag(plugins.TagData):
    def Init(self, node, isCloneInit):
        self.InitAttr(node, str, c4d.TCAST_IMPORT_PATH)
        self.InitAttr(node, bool, c4d.TCAST_IMPORT_TIME)
        self.InitAttr(node, bool, c4d.TCAST_IMPORT_RESET)
        node[c4d.TCAST_IMPORT_PATH] = ""
        node[c4d.TCAST_IMPORT_TIME] = False
        node[c4d.TCAST_IMPORT_RESET] = False

        return True

    def Message(self, node, type, data):
        if type == c4d.MSG_DESCRIPTION_COMMAND:
            if not data: return
            commandId = data['id'][0].id
            doc = c4d.documents.GetActiveDocument()
            if commandId == c4d.TCAST_IMPORT_BTN and "cast" in node[c4d.TCAST_IMPORT_PATH][-4:]:
                cast = Cast.load(node[c4d.TCAST_IMPORT_PATH])
                for root in cast.Roots():
                    for child in root.ChildrenOfType(Model):
                        print("Please Use Animation Nodes")
                    for child in root.ChildrenOfType(Animation):
                        importAnimationNode(doc, node, child)
                    for child in root.ChildrenOfType(Instance):
                        print("Please Use Animation Nodes")

            if commandId == c4d.TCAST_RESET_BTN:
                utilityResetBindPose(doc, node)

        return True


if __name__ == '__main__':
    fn = os.path.join(PLUGIN_RES_DIR, "icon", "icon.png")
    bmp = c4d.bitmaps.BaseBitmap()
    bmp.InitWith(fn)

    plugins.RegisterSceneLoaderPlugin(id=SCENE_LOADER_PLUGIN_ID,
                                            str=__pluginname__,
                                            info=0,
                                            g=CastLoader,
                                            description="fcastloader",
                                            )

    plugins.RegisterTagPlugin(id=TAG_PLUGIN_ID,
                                            str=__pluginname__,
                                            info=c4d.TAG_EXPRESSION | c4d.TAG_VISIBLE,
                                            g=CastTag,
                                            description="tcasttag",
                                            icon=bmp)
