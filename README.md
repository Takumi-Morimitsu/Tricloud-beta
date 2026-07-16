# Tricloud

[English](README.md) | [日本語](README.ja.md) | [Español](README.es.md)

Tricloud is a Windows beta desktop cloud storage app that incorporates ideas from Web3 and decentralized storage.

Tricloud is being developed around a simple question: does cloud storage have to depend only on large centralized storage providers?

Users can use cloud storage and can also contribute unused storage capacity from their own computers to the network. In the future, Tricloud aims to become a service in which many users contribute storage capacity and may receive rewards based on factors such as contributed capacity and availability.

The current public beta includes basic cloud storage features and an experimental storage-provider mode.

> \*\*Status:\*\* Public beta / early prototype  
> \*\*Version:\*\* `v0.1.0-beta`  
> \*\*Supported OS:\*\* Windows  
> \*\*Category:\*\* Cloud storage inspired by Web3 and decentralized storage  
> \*\*Stability:\*\* Experimental  
> \*\*Recommended use:\*\* Testing only  
> \*\*Important:\*\* Do not use this beta as the only storage location for important or irreplaceable files.

\---

## ⚠️ Important: No rewards are paid during this beta

# **Storage providers do not receive money, cryptocurrency, tokens, or any other reward during the current beta test.**

The storage-provider test is intended only to verify technical behavior, usability, connectivity, and stability.

Participating in the beta, contributing storage, or keeping a node online does not create any entitlement to future compensation.

\---

## Tricloud and Web3

Tricloud aims to let users contribute unused storage capacity from their computers so that other users can use that capacity as cloud storage.

It incorporates ideas associated with Web3 and decentralized services in the following ways:

* Users can contribute storage capacity to the network.
* People can participate as both storage users and storage providers.
* Storage capacity does not have to come only from one large storage operator.
* The service can use storage contributed by multiple participants.
* A reward system for storage providers may be introduced in the future.

However, **only the provision and use of storage are decentralized in Tricloud**.

Authentication, account information, file metadata, node management, storage placement, and other coordination functions are handled by a central management server. There is no plan to make the entire service fully decentralized.

Tricloud is therefore not a fully decentralized dApp. It is a cloud storage service that applies ideas from Web3 and decentralized storage specifically to the provision and use of storage.

\---

## Blockchain-related technologies

Tricloud currently does not use:

* Blockchain
* A proprietary token
* A user cryptocurrency wallet
* Smart contracts
* NFTs
* DAO-based governance
* Blockchain-based account or file management

These technologies are not considered necessary for the current Tricloud service, and there is no plan to implement them in the normally expected future development of the service.

There is also no plan to move the management server, authentication, metadata management, storage placement, or reward calculations onto a blockchain or smart contracts.

If Tricloud were one day to expand into its own computers, smartphones, or a substantially different platform, the necessary technologies could be reconsidered. Within the scope of the cloud storage service currently being developed, however, Tricloud does not plan to introduce blockchain, a proprietary token, a dedicated wallet, or smart contracts.

\---

### Possible support for existing cryptocurrencies

If existing cryptocurrencies eventually become as legally, technically, and operationally flexible as ordinary currencies, Tricloud may consider supporting them as one optional method for paying storage fees or receiving storage-provider rewards.

This would only add another payment or payout method alongside options such as ordinary currency, cards, or bank transfers. It would not mean:

* Issuing a Tricloud token
* Providing a Tricloud wallet
* Operating the service through smart contracts
* Storing file or account information on a blockchain
* Turning Tricloud into a blockchain application

\---

## Download

The `v0.1.0-beta` Windows build will be distributed through GitHub Releases.

* **Installer build:** The supported distribution format for this release
* **Portable build:** Not included in this release because the tested build currently fails at startup with `Invalid file descriptor to ICU data received.`

The Installer build includes the backend files and Python runtime required by the app. Testers normally do not need to install Python or Python packages separately.

In the two tested environments, installation and first launch completed within a few seconds. Actual startup time may vary by computer.

The beta is not code-signed. Windows SmartScreen therefore displays a warning when the installer is launched.

\---

## What you can test in this beta

The current beta includes:

* Account creation
* Login from the desktop app
* File uploads
* File downloads
* File and folder creation and management
* A desktop cloud-drive-style interface
* Automatic backup from the Backup Settings page
* Offline use of files and folders
* Storage contribution amount settings
* Starting and stopping a storage-provider node
* Experimental storage-provider functionality
* Node status monitoring
* HTTPS connections to the Tricloud beta server
* Reconnection handling after a temporary network interruption
* Running the Installer build on another Windows computer

Some features may still be incomplete, unstable, or temporarily unavailable.

\---

## Storage-provider mode

Storage-provider mode lets you contribute part of your computer's unused storage capacity to the Tricloud test network.

The current beta flow is:

1. Enter the amount of storage to contribute.
2. Save the setting.
3. Start storage contribution.
4. Check the node status.
5. Stop storage contribution when necessary.

The distributed build includes the required Python runtime and node backend, so users normally do not need to prepare Python manually.

If the internet or Wi-Fi connection is temporarily interrupted while storage contribution is active, the node attempts to reconnect to the DataServer after connectivity returns. Depending on the network environment, it may take some time for the node to return to an online state.

### Reward policy during the beta

# **No rewards are paid for storage contribution during the current beta.**

The test is intended to verify:

* Whether a node can start on other Windows computers
* Whether contribution can be started and stopped correctly
* Whether the node reconnects after a network interruption
* Whether contributed capacity is recognized correctly
* Whether the node can remain online for extended periods
* Whether the setup flow is understandable

Participation in the beta does not guarantee future payment.

\---

## Not yet completed

The following areas are experimental or not ready for general production use:

* Production-ready storage-provider functionality
* Payments to storage providers
* Production reward calculation and distribution
* Stripe Connect payout onboarding
* Mobile applications
* macOS and Linux desktop applications
* Production-level user support
* Long-term storage guarantees
* Independent security audits
* Large-scale performance testing

Blockchain, proprietary tokens, wallets, and smart contracts are not unfinished features. They are technologies that Tricloud does not currently plan to implement.

The storage-provider test is available, but no reward is paid during the current beta.

This beta should be treated as a technical preview rather than a production storage service.

\---

## Current architecture

The current Tricloud architecture broadly consists of:

* A Windows desktop app built with Electron, React, TypeScript, and Tailwind CSS
* A bundled Python runtime
* A bundled backend for the storage-provider node
* A FastAPI Control API
* PostgreSQL for account and file metadata
* A DataServer for storage-related communication
* Nginx for HTTPS access
* ZeroMQ communication between the DataServer and nodes
* A beta management server running on a Google Cloud VM
* Storage-provider nodes running on users' Windows computers

Tricloud combines a central management server with storage nodes contributed by users.

The central server manages authentication, metadata, node information, storage placement, and coordination. User nodes contribute storage capacity for file data.

There is no plan to replace the central management functions with blockchain technology.

\---

## Installation

### Installer build (`v0.1.0-beta`)

1. Download the Installer build from GitHub Releases.
2. Run the installer.
3. If Windows SmartScreen displays a warning, review the warning and choose to continue only if you downloaded the file from the official Tricloud GitHub Release.
4. Wait while the app, backend, and runtime are installed.
5. Launch Tricloud from the Start menu or desktop shortcut.
6. Create an account or log in.
7. Test uploading and downloading a small, non-sensitive file.

### Portable build

The Portable build is not distributed with `v0.1.0-beta` because the currently tested package does not start successfully and displays `Invalid file descriptor to ICU data received.`

### Python

Users normally do not need to install Python separately.

The Installer build includes the Python runtime and backend files required by storage-provider mode.

Older test builds may not contain all required files. Use the latest build from GitHub Releases.

\---

## Tested environments

`v0.1.0-beta` has been tested on two Windows 11 x64 computers using:

* 12th Gen Intel(R) Core(TM) i5-1240P
* Intel(R) Core(TM) i3-7020U

The following were confirmed in the tested environments:

* Installation and uninstallation
* Startup within a few seconds
* Account creation and login
* Small and moderately sized file uploads
* Downloads matching the original files
* Automatic backup reflecting file and folder changes
* Offline-use changes being reflected after reconnection
* Starting, stopping, restarting, and reconnecting a storage-provider node
* Normal app operation after completely closing and restarting Tricloud

Windows 10, Windows on ARM64, and other environments have not yet been verified. These processors are test environments, not minimum system requirements.

\---

## First test checklist

After launching the app, try:

1. Create a new account.
2. Log in.
3. Upload a small test file.
4. Download the file.
5. Close the app.
6. Start the app again.
7. Log in and confirm that the file list still appears.

To test storage-provider mode:

1. Enter a contribution amount.
2. Save the setting.
3. Start storage contribution.
4. Confirm that the node becomes online.
5. Stop contribution.
6. Start it again.
7. If practical, temporarily disconnect Wi-Fi.
8. Confirm whether the node reconnects after Wi-Fi returns.

Use a test environment that does not contain important data.

\---

## Reporting a problem

Please open a GitHub Issue and include:

* Windows version
* Tricloud version
* Confirm that you used the `v0.1.0-beta` Installer build
* What you were trying to do
* What actually happened
* Any visible error message
* The action performed immediately before the problem
* Whether the node was online or offline, if storage-provider mode was involved
* Whether a network interruption or reconnection occurred

Do not include:

* Passwords
* Authentication tokens
* API keys
* Personal information
* File contents
* Private keys or other confidential information

\---

## Known limitations

* The current confirmed test environment is Windows 11 x64 only. Windows 10 and Windows on ARM64 have not yet been verified.
* The application is unsigned, and Windows SmartScreen displays a warning.
* The Portable package is not included in `v0.1.0-beta` because it currently fails to start with `Invalid file descriptor to ICU data received.`
* Initial startup and runtime preparation may take longer on untested computers.
* No Microsoft Defender block was observed in the two tested environments, but other computers or security products may inspect or block the bundled Python runtime or node process.
* The management server may be unavailable temporarily because of maintenance, updates, restarts, or failures.
* Beta changes or database migrations may require accounts or stored test data to be deleted or reset.
* There is no SLA or guarantee for availability, retention duration, recovery, or restoration of data.
* Upload, download, automatic-backup, and offline-use behavior may change during the beta.
* Storage-provider mode is experimental.
* **No reward is paid for storage contribution during the current beta.**
* Reconnection after a network interruption may take time.
* Some UI elements and error messages are incomplete.
* Large-scale performance has not been fully tested.
* Tricloud is not a fully decentralized system.
* Do not use the beta for important, sensitive, or irreplaceable data.
* Do not use Tricloud as the only copy of any important file.

\---

## Security and privacy

Tricloud is still an early beta.

Do not upload:

* Files containing personal information
* Highly confidential files
* Business secrets
* Files that would be difficult to lose
* Files that do not exist anywhere else

The beta server is intended to test the basic service flow. Further security hardening and independent review remain future work.

\---

## Development stack

* Electron
* React
* TypeScript
* Tailwind CSS
* Python
* FastAPI
* PostgreSQL
* Nginx
* Google Cloud
* ZeroMQ
* electron-builder

The following technologies are not used and are not currently planned for the service:

* Blockchain
* Proprietary tokens
* Wallets
* Smart contracts
* NFTs
* DAOs

\---

## Roadmap

Planned work includes:

* Improving startup reliability on other Windows computers
* Improving Python runtime setup
* Stabilizing uploads and downloads
* Improving reconnection after network interruptions
* Improving error handling and logs
* Expanding desktop file management
* Improving storage-provider mode
* Improving contributed-capacity and availability measurements
* Designing storage-provider rewards
* Implementing payout functionality
* Improving account and usage pages
* Security hardening
* Considering macOS and Linux builds
* Contributor and developer documentation
* Considering existing cryptocurrency as an optional payment or payout method only if it becomes sufficiently practical

Even if cryptocurrency support is added, Tricloud does not plan to introduce a proprietary token, dedicated wallet, or smart contracts.

\---

## Feedback requested

### 1\. Running on other computers

* Did the app start without manually installing Python?
* Did you use the `v0.1.0-beta` Installer build?
* How long did the first startup take?
* Did Windows Defender or other security software block anything?
* Did the app work when installed or extracted to a different location?
* Did it continue to work after restarting the app?
* Were error messages understandable?

### 2\. Core functionality

* Did account creation and login work?
* Did file uploads and downloads work?
* Were file and folder operations understandable?
* Did automatic backup behave as expected?
* Was offline use understandable?
* Was performance acceptable and stable?

### 3\. UI and translations

* Is the layout understandable?
* Are buttons and menus placed naturally?
* Is the wording clear?
* Does the app feel natural as a Windows desktop application?
* **Please report any incorrect, unnatural, or unclear Japanese, English, or Spanish UI translation.**

### 4\. Placement of automatic backup

Automatic backup is currently operated mainly from the Backup Settings page.

Should it also be available from the file and folder context menu?

* Keep it only on the Backup Settings page
* Also add it to the context menu
* Make it available in both places
* Use a different interaction entirely

### 5\. Storage-provider mode

* Was the setup flow understandable?
* Could you start the node without separately installing Python?
* Was the contribution amount easy to understand?
* Was it easy to start and stop contribution?
* Was the node status understandable?
* Did the node reconnect after Wi-Fi was interrupted?
* Were the explanations and warnings sufficient?
* Would you use the feature if rewards were available in the future?

### 6\. Possible future reward model

Two possible reward models are being considered:

1. A metered model based on contributed capacity and availability
2. A lottery-like model in which contributed capacity and availability affect the chance of receiving a reward

Which model would be more attractive, or would you prefer another approach?

A lottery-like model may raise legal or regulatory issues depending on the country or region. Even if it receives more support, Tricloud may ultimately need to use the metered model after legal review.

\---

## Contributing

Tricloud is still an early beta, and its internal structure may change significantly. Bug reports and feedback are welcome.

Useful feedback includes:

* Reports from other Windows computers
* Installer installation and uninstallation results
* Bugs
* UI problems
* Installation problems
* Python runtime problems
* Unclear wording or translations
* Performance and stability problems
* Network reconnection problems
* Opinions on automatic-backup placement
* Opinions on storage-provider mode
* Opinions on future reward models

\---

## License

License: TBD

A formal license will be selected before broader publication.

Until a license is selected, no permission to use, redistribute, or modify the source code should be assumed.

\---

## Disclaimer

Tricloud is experimental software. Use it at your own risk.

Do not use this beta as the only storage location for important files.

Storage-provider mode and future reward models include concepts that are still being tested.

Participating in the beta, contributing storage, or keeping a node online does not guarantee future payment, token distribution, cryptocurrency, or any other benefit.

Tricloud does not plan to issue a proprietary token.

