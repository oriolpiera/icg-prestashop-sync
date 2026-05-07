def test_repository_has_architecture_document() -> None:
    from pathlib import Path

    assert Path("docs/architecture.md").exists()
