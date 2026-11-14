# Microsoft authentication

The application identifier must be configured locally. Select Microsoft sign-in and enter the identifier privately when prompted. The launcher opens Microsoft's sign-in page in the system browser and receives the authorization response on the local computer using the authorization code flow with PKCE. The launcher never asks for a Microsoft password. The application registration must allow personal Microsoft accounts, public clients and the `http://localhost` redirect URI. `consolelauncher login --device-code` remains an alternative when loopback browser sign-in is unavailable.

After approval, Console Launcher exchanges the Microsoft access token through Xbox Live and XSTS, obtains a Minecraft Services token, checks the Java entitlement (`game_minecraft` or `product_minecraft`) and retrieves the official Java profile's UUID and player name. A qualifying Game Pass entitlement is accepted through the same service checks; no purchase or license check is bypassed. An absent Java entitlement or profile produces a separate license/profile error.

Minecraft Services API approval may be required for the configured application. A server-side refusal is reported explicitly, without substituting a different application ID, offline identity or third-party authentication. `CONSOLECRAFT_CLIENT_ID` remains an environment override.

OAuth, Xbox account/region/family-access problems, Minecraft Services failures, application access refusal and absent Java entitlement are represented by separate error types and user-facing messages. Remote response descriptions and HTTP exceptions are not printed because they can contain private data.

Application identifiers and refresh tokens are stored in Windows Credential Manager, macOS Keychain, Linux Secret Service/libsecret or in process memory when secure system storage is unavailable. Access tokens are kept in memory. No password or token is written to `config.json`, `profiles.json`, profile export archives, launcher logs or exception messages. A valid in-process session is reused; expired sessions refresh automatically before a Microsoft launch. Logout removes the local refresh credential, but does not revoke the application's consent in the Microsoft account.

Credentials remain scoped to the launcher data directory and Client ID, retaining Console Launcher 1.0.0's namespace mechanism. Changing the Client ID selects a different credential record and may require signing in again. Without secure system storage, use the interactive menu for both sign-in and launching in one process.

Protocol and approval references:

- [Microsoft desktop app registration](https://learn.microsoft.com/en-us/entra/identity-platform/scenario-desktop-app-registration)
- [Minecraft API application approval notes](https://minecraft-launcher-lib.readthedocs.io/en/stable/tutorial/microsoft_login.html)

Automated tests use synthetic mocked tokens only. Real Microsoft account sign-in is not claimed as tested without a completed live flow and the necessary server-side approval.
