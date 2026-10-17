from dragonpilot.settings import tr

ITEMS = [
  {
    "section": "UI",
    "key": "dp_ui_display_mode",
    "type": "text_spin_button_item",
    "title": lambda: tr("Display Mode"),
    "description": lambda: tr("Std.: Stock behavior.<br>MAIN+: ACC MAIN on = Display ON.<br>OP+: OP enabled = Display ON.<br>MAIN-: ACC MAIN on = Display OFF<br>OP-: OP enabled = Display OFF.<br>TR-9.2: TURZX USB cluster, 1920x462.<br>TR-12.3: TURZX USB cluster, 1920x720.<br>Only TR modes enable the external cluster driver and background process. In TR modes, the main display wakes only on touch and sleeps after the interaction timeout. Changes take effect without a restart."),  # noqa: E501
    "options": [lambda: tr("Std."), lambda: tr("MAIN+"), lambda: tr("OP+"), lambda: tr("MAIN-"), lambda: tr("OP-"),
                lambda: tr("TR-9.2"), lambda: tr("TR-12.3")],
    "default": "0",
    "flags": "PERSISTENT",
    "param_type": "INT",
  },
]
