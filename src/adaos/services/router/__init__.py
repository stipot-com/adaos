from .media_routes import resolve_media_route_intent


def __getattr__(name: str):
    if name == "RouterService":
        from .service import RouterService

        return RouterService
    raise AttributeError(name)

__all__ = ["RouterService", "resolve_media_route_intent"]

