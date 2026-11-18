"""Sanitized, stage-specific failures; never include model output or credentials."""

from jobradar.llm.client import LLMError


class AnalysisError(LLMError):
    def __init__(self, code, stage, message, *, path=None, rule=None, details=None):
        super().__init__(message,code=code)
        self.stage, self.path, self.rule = stage, path, rule
        self.details = details

    def diagnostic(self, severity="error"):
        return {"code":self.code,"stage":self.stage,"message":str(self),
                "path":self.path,"rule":self.rule,"severity":severity,"details":self.details}
