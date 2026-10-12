class AccountError(Exception):
    """User-facing tool failure: bad alias, missing grant, provider rejection.

    Tools catch this and return {"ok": False, "error": str(e)}; the message is
    written for the calling model, so it should say what to do next.
    """


class CapabilityError(Exception):
    """The provider does not support the capability the tool asked for.

    Raised by provider dispatch when a tool like edit_post is called on
    Instagram, or get_insights before the account has granted read_insights.
    """
