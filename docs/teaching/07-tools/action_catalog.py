"""List the session-action types currently shipped by EasyCat."""

from __future__ import annotations

import json
from inspect import getmembers, isclass

from easycat.session import actions as action_module
from easycat.session.actions import CoreSessionActionExecutor, SessionAction


def catalog() -> list[dict[str, object]]:
    """Discover concrete action dataclasses and core-executor coverage."""
    core = CoreSessionActionExecutor()
    action_classes = sorted(
        (
            action_class
            for _, action_class in getmembers(action_module, isclass)
            if action_class is not SessionAction
            and issubclass(action_class, SessionAction)
            and action_class.__module__ == action_module.__name__
        ),
        key=lambda action_class: action_class.action_type.value,
    )
    return [
        {
            "action_class": action_class.__name__,
            "action_type": action_class.action_type.value,
            "core_supported": core.supports(action_class()),
        }
        for action_class in action_classes
    ]


if __name__ == "__main__":
    actions = catalog()
    print(json.dumps({"count": len(actions), "actions": actions}, indent=2, sort_keys=True))
