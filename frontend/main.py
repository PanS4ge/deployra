from loguru import logger
import statics
import loguru
import sys
import fastapi
import nicegui
import os
import uvicorn

logger.opt(colors=True).info("cwd: <red>" + os.getcwd() + "</red>")

logger.opt(colors=True).info("Using <yellow>python</yellow> version " + str(sys.version_info.major) + "." + str(sys.version_info.minor) + " (" + str(sys.version_info.releaselevel) + ")")
for mod in [statics, fastapi, nicegui, loguru]:
    logger.opt(colors=True).info(f"Using <yellow>{mod.__name__}</yellow> version {mod.__version__}")

if(statics.uvicorn):
    uvicorn.run("webpage:app", host="0.0.0.0", port=statics.port, reload=False)
else:
    import webpage