# Tricloud v0.1.0-beta Release Notes

## Distribution

- Windows 11 x64 Installer build
- Portable build is not distributed in this release because the tested package fails at startup with `Invalid file descriptor to ICU data received.`
- The Installer build is unsigned, so Microsoft Defender SmartScreen may display a warning.

## Verified test environments

The Installer build was tested on two Windows 11 x64 computers using:

- 12th Gen Intel Core i5-1240P
- Intel Core i3-7020U

The following behavior was confirmed in the tested environments:

- Installation and uninstallation
- Account creation and login
- Upload and download, including content consistency after download
- Automatic backup updates for files and folders
- Offline-use synchronization after reconnection
- Starting, stopping, restarting, and reconnecting a storage-provider node
- Normal operation after completely closing and restarting the Tricloud app

## Important beta limitations

- **No money, cryptocurrency, token, or other reward is paid for storage contribution during this beta.**
- Windows 10 and Windows on ARM64 have not been verified.
- This is not production storage and has no SLA, retention guarantee, or recovery guarantee.
- Stored data and accounts may be reset during beta maintenance or schema changes.
- Do not use the beta as the only storage location for important or sensitive files.
