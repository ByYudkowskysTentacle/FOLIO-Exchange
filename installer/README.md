# Windows installer

The installer sets up FOLIO-Exchange on a Windows 10 or 11 PC, or updates it. It is a short wizard in a console window: it asks two questions, installs the program, puts a launcher on the desktop and opens the FOLIO-Exchange page. You connect to FOLIO and add vendor feeds on that page. The installer itself never asks for, reads or stores a password, adds nothing to PATH and writes nothing to the registry.

## What it does

1. **Finds Python** 3.10 or newer. If there is none, it offers to install Python 3.12 with winget (the Windows Package Manager), for your Windows user only, so that needs no administrator rights either. Without winget, it points you to python.org. The Microsoft Store version of Python is passed over, because Windows keeps the files it makes in a hidden private folder.
2. **Asks two questions.** Press Enter to keep the suggestion in [brackets].
   - The install folder, by default `%LOCALAPPDATA%\FOLIO-Exchange` (for example `C:\Users\you\AppData\Local\FOLIO-Exchange`). It warns you if the folder is inside OneDrive or Dropbox: syncing breaks the cache and lock files that FOLIO-Exchange keeps while it works.
   - A short name for your library's site, by default `mylibrary`: letters, digits and hyphens.
3. **Installs FOLIO-Exchange** in `<install folder>\program`, a Python environment of its own. It downloads FOLIO-Exchange from GitHub as a zip file, so Git is not needed. If FOLIO-Exchange is already there, it updates it.
4. **Makes a launcher**, `FOLIO-Exchange (<name>).cmd`, on your desktop. Double-click it to open the page.
5. **Offers to set up scheduled runs**: Windows then starts FOLIO-Exchange every hour to run the feeds that are due. Windows asks for your Windows password for this, so the runs also happen when nobody is signed in. You can say no and do it later.
6. **Opens the page** in your browser. The page asks for the FOLIO connection – the API address, the tenant id, a user name and its password – and is also where you add vendor feeds and their passwords.

At the end it lists where everything is, and anything still to do.

| What | Where |
| --- | --- |
| The program | `<install folder>\program` |
| Your library's site: settings, feeds, run history, cache, the files built for vendors, logs | `<install folder>\<name>` |
| The launcher | `FOLIO-Exchange (<name>).cmd` on your desktop |
| The scheduled task | Task Scheduler, named `FOLIO-Exchange (<name>)` |
| Passwords | Windows Credential Manager, in entries whose names start with `folio-exchange/` |

## Install

You need a Windows 10 or 11 PC that can reach github.com and pypi.org (and, to use FOLIO-Exchange, your FOLIO API). You do not need administrator rights, for FOLIO-Exchange or for the Python the installer can add. Install it signed in to Windows as the person who will use FOLIO-Exchange: the program, the saved passwords and the scheduled task all belong to that Windows user.

1. Download the installer: either the two files `install.cmd` and `install.ps1` from this folder, into the same folder on your PC, or the whole repository as a zip (**Code > Download ZIP** on the repository's GitHub page).
2. If you downloaded the zip, extract it (right-click it, then **Extract All**). `install.cmd` does not work from inside the zip.
3. Open the `installer` folder and double-click `install.cmd`. If Windows warns that the file came from the internet, choose **Run** (or **More info**, then **Run anyway**).
4. Answer the questions. At the end the window stays open so you can read the summary; press a key to close it.

### For IT staff

The same script runs without questions:

```
powershell -ExecutionPolicy Bypass -File install.ps1 -Yes -SiteName mylibrary
```

`-Yes` accepts every default. That includes setting up scheduled runs, for which Windows still asks for the user's Windows password in the console. For a fully unattended install add `-NoSchedule -NoStart`, and set up the scheduled runs later, as the user, with the command the installer prints. Run the installer as the Windows user who will use FOLIO-Exchange, not as a separate administrator account. Started with `-NonInteractive` and without `-Yes`, as some deployment tools do, it takes the suggested folder and name and answers no to every yes/no question, so it installs no Python and sets up no scheduled runs.

Exit codes: `0` when everything is done; `1` when the installer stopped; `2` when FOLIO-Exchange is installed but a step is still to do (listed at the end of the output).

## Parameters

| Parameter | What it does |
| --- | --- |
| `-InstallDir <folder>` | The install folder. Default: `%LOCALAPPDATA%\FOLIO-Exchange`. |
| `-SiteName <name>` | The site's short name: letters, digits and hyphens. Default: `mylibrary`. |
| `-Source <url or path>` | Install from somewhere else: a URL that pip understands (a zip, or `git+https://...`, which needs Git) or a local folder or archive. Default: the `main` branch on GitHub, as a zip. |
| `-Ref <branch or tag>` | Install this branch or tag of the GitHub repository instead of `main`. Not together with `-Source`. |
| `-LauncherFolder <folder>` | Put the launcher in this folder instead of on the desktop. |
| `-NoStart` | Do not open the page at the end. |
| `-NoSchedule` | Do not set up scheduled runs, and do not ask about them. |
| `-Yes` | Accept every default without asking. Without it, when nobody can answer (no console), every yes/no question is answered no. |

`Get-Help .\install.ps1 -Full` shows the same in PowerShell. With `install.cmd`, put the parameters after it, for example `install.cmd -SiteName main-campus`.

## Update

Run the installer again and give the same install folder and site name. It replaces the program in `<install folder>\program` and keeps everything else: the site's settings, feeds, run history and cache, the passwords in Credential Manager, and the scheduled task, which still points at the same program.

Close the page's black window first, and choose a time when no run is due. If FOLIO-Exchange is still running from that folder, the installer says so and asks before it goes on.

## Uninstall

1. Close the page's black window, if it is open.
2. If scheduled runs were set up, remove the task, in PowerShell or Command Prompt, with your site's name in place of `mylibrary`:

   ```
   schtasks /Delete /TN "FOLIO-Exchange (mylibrary)"
   ```

3. Delete the launcher, `FOLIO-Exchange (<name>).cmd`, from your desktop.
4. Delete the install folder, by default `%LOCALAPPDATA%\FOLIO-Exchange`. This also deletes the site: its settings, run history, cache and the files built for vendors. Copy anything you want to keep first.
5. Remove the saved passwords: open **Credential Manager** from the Start menu, choose **Windows Credentials**, and remove each entry whose name starts with `folio-exchange/`.

Python stays installed; remove it in **Settings > Apps** if nothing else needs it.

## If something goes wrong

| Problem | What to do |
| --- | --- |
| No Python was found | Let the installer install it with winget, or install Python 3.10 or newer from python.org, then run the installer again. |
| FOLIO-Exchange could not be downloaded | The PC cannot reach github.com or pypi.org. Ask IT whether a firewall or proxy blocks them; pip uses a proxy set in the `HTTPS_PROXY` environment variable. |
| pip mentions a path that is too long | Choose a shorter install folder, such as `C:\FOLIO-Exchange`. |
| FOLIO-Exchange is running | Close the page's black window, wait for any run to finish, then run the installer again. |
| The launcher or scheduled runs could not be set up | The installer prints the command that does it; run it later in PowerShell. FOLIO-Exchange is installed either way. |
