class MediaLabError(Exception):
    """An expected, user-actionable failure with a stable category."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
