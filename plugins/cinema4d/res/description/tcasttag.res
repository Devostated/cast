CONTAINER tcasttag
{
    NAME tcasttag;
    INCLUDE Texpression;

    GROUP ID_TAGPROPERTIES
    {
        FILENAME TCAST_IMPORT_PATH { ANIM OFF; }
        BOOL TCAST_IMPORT_TIME { ANIM OFF; }
        BOOL TCAST_IMPORT_RESET { ANIM OFF; CUSTOMGUI BOOL; }

		SEPARATOR { }

        GROUP
        {
            LAYOUTGROUP; COLUMNS 2;

			GROUP
			{
				BUTTON TCAST_IMPORT_BTN { }
			}

			GROUP
			{
				BUTTON TCAST_RESET_BTN { }
			}
        }
    }
}
