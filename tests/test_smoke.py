def test_repository_has_architecture_document() -> None:
    from pathlib import Path

    assert Path("docs/architecture.md").exists()


def test_repository_has_openspec_bootstrap() -> None:
    from pathlib import Path

    assert Path("openspec/README.md").exists()
    assert Path("openspec/changes/README.md").exists()
