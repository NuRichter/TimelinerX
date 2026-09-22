"""Error hierarchy. Every category maps to a stable CLI exit code."""

from __future__ import annotations


class TimelinerXError(Exception):
    """Base class for expected, user-reportable failures."""

    exit_code = 1
    category = "general"

    def __init__(self, message: str, *, hint: str | None = None):
        super().__init__(message)
        self.hint = hint

    def to_dict(self) -> dict:
        return {"category": self.category, "message": str(self), "hint": self.hint,
                "exit_code": self.exit_code}


class UsageError(TimelinerXError):
    exit_code = 2
    category = "usage"


class ImportFailedError(TimelinerXError):
    exit_code = 10
    category = "import"


class UnsupportedFormatError(ImportFailedError):
    exit_code = 11
    category = "import_unsupported_format"


class InputTooLargeError(ImportFailedError):
    exit_code = 12
    category = "import_too_large"


class NoDataError(ImportFailedError):
    exit_code = 13
    category = "import_no_data"


class ProjectError(TimelinerXError):
    exit_code = 20
    category = "project"


class FfmpegUnavailableError(TimelinerXError):
    exit_code = 30
    category = "ffmpeg_unavailable"


class EncoderUnavailableError(TimelinerXError):
    exit_code = 31
    category = "encoder_unavailable"


class FallbackRequiresConfirmationError(TimelinerXError):
    """Raised when a degradation is needed but the caller has not consented."""

    exit_code = 32
    category = "fallback_requires_confirmation"

    def __init__(self, message: str, decision=None, **kw):
        super().__init__(message, **kw)
        self.decision = decision


class InsufficientStorageError(TimelinerXError):
    exit_code = 40
    category = "insufficient_storage"


class TileProviderError(TimelinerXError):
    exit_code = 50
    category = "map_tiles"


class RenderCancelledError(TimelinerXError):
    exit_code = 60
    category = "cancelled"


class RenderFailedError(TimelinerXError):
    exit_code = 61
    category = "render_failed"


class VerificationFailedError(TimelinerXError):
    exit_code = 62
    category = "verification_failed"


class PluginError(TimelinerXError):
    exit_code = 70
    category = "plugin"
