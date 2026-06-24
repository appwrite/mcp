def main():
    """Main entry point for the package."""
    from .server import main as _main

    return _main()


__all__ = ["main"]
