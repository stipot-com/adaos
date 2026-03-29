from __future__ import annotations

SEED: dict = {
    "ui": {
        "application": {
            "version": "0.2",
            "desktop": {
                "topbar": [],
                "iconTemplate": {"icon": "apps-outline"},
                "widgetTemplate": {"style": {"minWidth": 240}},
                "pageSchema": {
                    "id": "desktop",
                    "title": "Desktop",
                    "layout": {
                        "type": "single",
                        "areas": [{"id": "main", "role": "main"}],
                    },
                    "widgets": [
                        {
                            "id": "desktop-icons",
                            "type": "collection.grid",
                            "area": "main",
                            "title": "Icons",
                            "inputs": {"columns": 6},
                            "dataSource": {
                                "kind": "y",
                                "transform": "desktop.icons",
                            },
                            "actions": [
                                {
                                    "on": "select",
                                    "type": "callHost",
                                    "target": "desktop.scenario.set",
                                    "params": {
                                        "scenario_id": "$event.scenario_id",
                                    },
                                },
                                {
                                    "on": "select",
                                    "type": "openModal",
                                    "params": {"modalId": "$event.action.openModal"},
                                },
                            ],
                        },
                        {
                            "id": "desktop-widgets",
                            "type": "desktop.widgets",
                            "area": "main",
                            "title": "Widgets",
                            "dataSource": {
                                "kind": "y",
                                "transform": "desktop.widgets",
                            },
                        },
                    ],
                },
            },
            "modals": {
                "apps_catalog": {
                    "title": "Available Apps",
                    "schema": {
                        "id": "apps_catalog",
                        "layout": {
                            "type": "single",
                            "areas": [{"id": "main", "role": "main"}],
                        },
                        "widgets": [
                            {
                                "id": "apps-list",
                                "type": "collection.grid",
                                "area": "main",
                                "title": "Apps",
                                "dataSource": {
                                    "kind": "y",
                                    "path": "data/catalog/apps",
                                },
                                "actions": [
                                    {
                                        "on": "select",
                                        "type": "callHost",
                                        "target": "desktop.toggleInstall",
                                        "params": {
                                            "type": "app",
                                            "id": "$event.id",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                },
                "widgets_catalog": {
                    "title": "Available Widgets",
                    "schema": {
                        "id": "widgets_catalog",
                        "layout": {
                            "type": "single",
                            "areas": [{"id": "main", "role": "main"}],
                        },
                        "widgets": [
                            {
                                "id": "widgets-list",
                                "type": "collection.grid",
                                "area": "main",
                                "title": "Widgets",
                                "dataSource": {
                                    "kind": "y",
                                    "path": "data/catalog/widgets",
                                },
                                "actions": [
                                    {
                                        "on": "select",
                                        "type": "callHost",
                                        "target": "desktop.toggleInstall",
                                        "params": {
                                            "type": "widget",
                                            "id": "$event.id",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                },
            },
            "registry": {
                "widgets": [],
                "modals": [],
            },
        }
    },
    "data": {
        "catalog": {
            "apps": [],
            "widgets": [],
        },
        "installed": {"apps": [], "widgets": []},
    },
}
