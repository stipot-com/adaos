"""
NLU runtime glue for AdaOS.

Importing this package is enough to register all NLU-related event
subscriptions (see dispatcher.py). The actual NLU engine lives
outside the hub; it publishes ``nlp.intent.detected`` events that
are then mapped to scenario/skill actions here.
"""

from . import dispatcher as _dispatcher  # noqa: F401
from . import pipeline as _pipeline  # noqa: F401
from . import rasa_service_bridge as _rasa_service_bridge  # noqa: F401
from . import rasa_training_bridge as _rasa_training_bridge  # noqa: F401
from . import trace_store as _trace_store  # noqa: F401
from . import teacher_bridge as _teacher_bridge  # noqa: F401
