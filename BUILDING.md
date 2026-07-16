# Building Tricloud

## Development

1. Copy `.env.example` to `.env.local` and configure the local or beta endpoints.
2. Install dependencies with `npm ci`.
3. Start the Electron development environment with `npm run electron:dev`.

## Windows Installer

The supported public release format for `v0.1.0-beta` is the NSIS Installer build for Windows x64.

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build-windows-installer-with-python.ps1
```

The build process uses:

- `build/prepare-python-runtime.ps1`
- Python embeddable package `3.11.9` for Windows x64
- `pyzmq 27.1.0` for CPython 3.11 / Windows x64

The preparation script downloads the required archives over HTTPS, creates `build/runtime/`, and validates that the bundled Python runtime can import `zmq`.

Generated files under the following paths are intentionally excluded from Git:

```text
build/runtime/
build/python-embed/
build/python-installer/
build/python-wheels/
build/*validation*.log
release/
dist/
```

Do not commit generated runtimes, downloaded installers or wheels, validation logs, or release binaries.
