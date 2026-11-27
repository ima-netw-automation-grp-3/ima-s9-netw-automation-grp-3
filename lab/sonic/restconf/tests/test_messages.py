from restconf.messages import gcu_message, libyang_message

LEAFREF = (
    "failed to parse data tree: Invalid leafref value \"Ethernet99\" - no target instance "
    "\"/port:sonic-port/port:PORT/port:PORT_LIST/port:name\" with the same value.: Data path: "
    "/sonic-interface:sonic-interface/INTERFACE/INTERFACE_LIST[name='Ethernet99']/name (line 1)"
)

# sortie réelle de « config apply-patch » (stdout puis stderr) sur un mtu invalide
GCU_MTU = """** DRY RUN EXECUTION **
Failed to apply patch due to: Validate json patch: [{'op': 'replace', 'path': '/PORT/Ethernet0/mtu', 'value': 'abc'}] failed due to: Data Loading Failed
All Keys are not parsed in PORT
dict_keys(['Ethernet0'])
exceptionList:["invalid literal for int() with base 10: 'abc'"]
Usage: config apply-patch [OPTIONS] PATCH_FILE_PATH
Try 'config apply-patch -h' for help.

Error: Validate json patch: [{'op': 'replace', 'path': '/PORT/Ethernet0/mtu', 'value': 'abc'}] failed due to: Data Loading Failed
All Keys are not parsed in PORT
dict_keys(['Ethernet0'])
exceptionList:["invalid literal for int() with base 10: 'abc'"]
sonic_yang(3):All Keys are not parsed in PORT
"""


def test_libyang_message_splits_path():
    message, path = libyang_message(LEAFREF)
    assert message.startswith('Invalid leafref value "Ethernet99"') and message.endswith("with the same value")
    assert path == "/sonic-interface:sonic-interface/INTERFACE/INTERFACE_LIST[name='Ethernet99']/name"
    assert libyang_message("Data Loading Failed\nquelque chose") == ("quelque chose", None)


def test_gcu_message():
    text = gcu_message(GCU_MTU)
    assert "Validate json patch" not in text and "Usage" not in text
    assert text.startswith("All Keys are not parsed in PORT")
    assert "invalid literal for int()" in text
    assert gcu_message("Failed to apply patch due to: boom\n") == "boom"
    assert gcu_message("autre chose") == "autre chose"
