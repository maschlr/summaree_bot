from asyncio import CancelledError


class NoActivePremium(Exception):
    pass


class EmptyTranscription(CancelledError):
    pass
