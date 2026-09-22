"""TimelinerX — turn Google Timeline exports into cinematic animated map videos.

Made with NuRichter Workspace.

Portions of the timeline parsing, projection, outlier filtering, pacing and
camera algorithms are adapted from Google Timeline Visualizer by mahlernim
(MIT License). See THIRD_PARTY_NOTICES.md.
"""

__version__ = "1.0.0"

# Bumped whenever a change can alter rendered pixels for identical inputs.
# Embedded in every project and every library entry so output differences
# between versions can be audited.
RENDER_ENGINE_VERSION = "tlx-2.0.0"

APP_NAME = "TimelinerX"
APP_ID = "TimelinerX"
LEGACY_APP_ID = "NuRichterTimeliner"   # data folder of the 0.x releases (migrated on first start)
AUTHOR = "NuRichter"
AUTHOR_WORKSPACE = "NuRichter Workspace"
LINKEDIN_URL = "https://www.linkedin.com/in/nurichter/"
GITHUB_URL = "https://github.com/NuRichter"
