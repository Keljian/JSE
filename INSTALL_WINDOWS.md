# Installing the JSE Windows beta

JSE is currently distributed as an unsigned Windows beta. The installer does not
have a paid code-signing certificate yet, so Microsoft Defender SmartScreen may
show an **Unknown publisher** warning. This warning does not mean SmartScreen
found malware; it means Windows cannot verify a publisher certificate.

## Before installing

1. Download JSE only from the project's official release page.
2. Compare the installer's SHA-256 checksum with the checksum published alongside
   the release. In PowerShell, run:

   ```powershell
   Get-FileHash .\JSE-1.0.0-beta.1-x64-unsigned-beta.exe -Algorithm SHA256
   ```

3. Install Google Chrome if it is not already installed. Browser-based searchers
   use Chrome; Selenium Manager obtains the compatible driver when needed.
4. Choose one local model runtime—there is no need to install both. Install either
   [LM Studio](https://lmstudio.ai/download) or
   [Ollama](https://ollama.com/download/windows), download a chat/instruct model,
   and start its local server. JSE uses local AI for job matching. The onboarding
   wizard saves the matching endpoint preset; Settings includes install links,
   endpoint presets, and a connection test.

## SmartScreen click-through

1. Open the downloaded installer.
2. If **Windows protected your PC** appears, confirm that the app is **JSE** and
   that your checksum matches the official release.
3. Select **More info**.
4. Select **Run anyway**.

Do not disable SmartScreen globally and do not continue if the file name, source,
or checksum is unexpected.

## Installer choices

The beta installs for the current Windows user and does not require an
administrator account. You may choose the installation directory. JSE keeps its
database, resumes, templates, and generated application files inside that JSE
installation tree, so back up or move the complete folder rather than copying
only the executable.

On first launch, the setup wizard checks the bundled runtime and Chrome, asks for
a lane name and base `.docx` resume, then guides you through LM Studio or Ollama.
Cloud AI providers and searchers can be configured later in **Settings**.

Node.js, npm, Python, Electron, and Python packages are included in the installer.
Microsoft Word is not required unless you choose to import a legacy `.doc` file.

## Uninstalling or upgrading

Back up the JSE installation folder before uninstalling or replacing a beta.
Because the local database and documents live with the application, deleting the
installation tree deletes that local data too.
