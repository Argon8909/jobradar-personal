"""Sanitized, stage-specific failures; never include model output or credentials."""

from jobradar.llm.client import LLMError


class AnalysisError(LLMError):
    def __init__(self, code, stage, message, *, path=None, rule=None):
        super().__init__(message,code=code)
        self.stage, self.path, self.rule = stage, path, rule

    def diagnostic(self, severity="error"):
        return {"code":self.code,"stage":self.stage,"message":str(self),
                "path":self.path,"rule":self.rule,"severity":severity}
