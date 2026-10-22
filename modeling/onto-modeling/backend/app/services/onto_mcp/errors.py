class McpError(Exception):
    def __init__(self, code, message, status=400, **details):
        super().__init__(message)
        self.code, self.status, self.details = code, status, details

    def public(self):
        return {"code": self.code, "message": str(self), "details": self.details,
                "suggestions": [], "retryable": self.status == 409}
