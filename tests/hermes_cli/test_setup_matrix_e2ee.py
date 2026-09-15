"""Test that setup.py has shutil available for Matrix E2EE auto-install."""
import ast



def _parse_setup_imports():
    """Parse setup.py and return MODULE-LEVEL import names.

    ``tree.body``, not ``ast.walk(tree)``: walk descends into function bodies,
    so a deferred ``import shutil`` inside some unrelated helper (setup.py has
    one) satisfied the assertion below and the module-level import could be
    deleted with the test still green — which is precisely the NameError this
    test exists to catch. Only top-level statements count as "imported at
    module level".
    """
    with open("hermes_cli/setup.py", encoding="utf-8") as f:
        tree = ast.parse(f.read())
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add(alias.name)
    return names


class TestSetupShutilImport:
    def test_shutil_imported_at_module_level(self):
        """shutil must be imported at module level so setup_gateway can use it
        for the mautrix auto-install path."""
        names = _parse_setup_imports()
        assert "shutil" in names, (
            "shutil is not imported at the top of hermes_cli/setup.py. "
            "This causes a NameError when the Matrix E2EE auto-install "
            "tries to call shutil.which('uv')."
        )
