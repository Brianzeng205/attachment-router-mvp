class DriveError(RuntimeError):
    """Base error for safe, user-actionable Drive integration failures."""


class DriveAuthenticationError(DriveError):
    pass


class DriveFolderError(DriveError):
    pass


class DrivePermissionError(DriveError):
    pass


class DriveUploadError(DriveError):
    pass


class InvalidFilenameError(DriveError):
    pass


class ClassifierError(RuntimeError):
    """Base error for recoverable Claude classification failures."""


class ClassifierAuthenticationError(ClassifierError):
    pass


class ClassifierRateLimitError(ClassifierError):
    pass


class ClassifierAPIError(ClassifierError):
    pass


class ClassifierResponseError(ClassifierError):
    pass


class InboxAnalyzerError(RuntimeError):
    """Base error for recoverable inbox-analysis failures."""


class InboxAnalyzerAuthenticationError(InboxAnalyzerError):
    pass


class InboxAnalyzerAPIError(InboxAnalyzerError):
    pass


class InboxAnalyzerResponseError(InboxAnalyzerError):
    pass


class ConversationAnalyzerError(RuntimeError):
    """Base error for recoverable conversation-analysis failures."""


class ConversationAnalyzerAuthenticationError(ConversationAnalyzerError):
    pass


class ConversationAnalyzerAPIError(ConversationAnalyzerError):
    pass


class ConversationAnalyzerResponseError(ConversationAnalyzerError):
    pass


class GmailError(RuntimeError):
    """Base error for Gmail polling failures."""


class GmailAuthenticationError(GmailError):
    pass


class GmailRateLimitError(GmailError):
    pass


class GmailAPIError(GmailError):
    pass


class GmailMessageError(GmailError):
    pass


class GmailAttachmentError(GmailError):
    pass


class GmailPayloadError(GmailError):
    pass
