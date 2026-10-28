<#
.SYNOPSIS
Installs or updates FOLIO-Exchange for one library on this Windows computer.

.DESCRIPTION
A short wizard for library staff. It:

  1. finds Python 3.10 or newer, and offers to install Python 3.12 with winget if there is none
     (for this Windows user only, so no administrator rights are needed);
  2. asks for the install folder and a short name for this library's site;
  3. installs FOLIO-Exchange in <install folder>\program, or updates it if it is already there;
  4. makes a launcher, "FOLIO-Exchange (<name>).cmd", on the desktop: double-click it to open the page;
  5. offers to have Windows start FOLIO-Exchange every hour, so feeds run when they are due;
  6. opens the FOLIO-Exchange page, where you connect to FOLIO and add vendor feeds.

The library's settings, run history and files live in <install folder>\<name>. Running the installer
again updates FOLIO-Exchange and keeps them.

The installer never asks for, reads or stores a FOLIO or vendor password: those are typed on the page.
It writes nothing to the registry and does not need administrator rights.

Exit codes: 0 when everything is done, 1 when the installer stopped, 2 when FOLIO-Exchange is installed
but a step is still to do (the installer lists it at the end).

.PARAMETER InstallDir
The install folder. Default: %LOCALAPPDATA%\FOLIO-Exchange. Avoid OneDrive and Dropbox folders.

.PARAMETER SiteName
A short name for this library's site: letters, digits and hyphens, for example mylibrary or
main-campus. The site folder is <install folder>\<name>. Default: mylibrary.

.PARAMETER Source
Where to install FOLIO-Exchange from: a URL that pip understands, or a local folder or archive.
Default: the main branch on GitHub, as a zip file, so Git is not needed.

.PARAMETER Ref
A branch or tag of the GitHub repository to install instead of main. Cannot be combined with -Source.

.PARAMETER LauncherFolder
Put the launcher in this folder instead of on the desktop. The folder is made if it is missing.

.PARAMETER NoStart
Do not open the FOLIO-Exchange page at the end.

.PARAMETER NoSchedule
Do not set up scheduled runs, and do not ask about them.

.PARAMETER Yes
Accept every default without asking. That includes setting up scheduled runs, for which Windows still
asks for the Windows password, unless -NoSchedule is given too. Without -Yes, when nobody can answer
(no console, or PowerShell started with -NonInteractive), questions take their suggestion and yes/no
questions are answered no.

.EXAMPLE
.\install.ps1
Asks a few questions, then installs or updates FOLIO-Exchange.

.EXAMPLE
powershell -ExecutionPolicy Bypass -File install.ps1 -Yes -SiteName mylibrary
Installs with the defaults for the site "mylibrary" without asking.

.EXAMPLE
powershell -ExecutionPolicy Bypass -File install.ps1 -Yes -NoSchedule -NoStart -SiteName mylibrary
Installs without asking anything at all, for a scripted install. Scheduled runs are set up later.

.LINK
https://github.com/ByYudkowskysTentacle/FOLIO-Exchange
#>
[CmdletBinding(PositionalBinding = $false)]
param(
    [string]$InstallDir,
    [string]$SiteName,
    [string]$Source,
    [string]$Ref,
    [string]$LauncherFolder,
    [switch]$NoStart,
    [switch]$NoSchedule,
    [switch]$Yes
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$RepoUrl = 'https://github.com/ByYudkowskysTentacle/FOLIO-Exchange'
$DefaultSource = $RepoUrl + '/archive/refs/heads/main.zip'
$WingetPython = 'Python.Python.3.12'
$StepCount = 6
$script:InputClosed = $false
$script:ToDo = New-Object 'System.Collections.Generic.List[string]'

# ---------------------------------------------------------------- output

function Write-Text([string]$Text, [string]$Color = '', [int]$Indent = 0, [int]$Hanging = 0) {
    # Wraps a paragraph at word boundaries, so the console does not split words. Lines after the first are
    # indented $Hanging more than the first.
    $width = 80
    try {
        $w = $Host.UI.RawUI.WindowSize.Width
        if ($w -ge 40) { $width = [Math]::Min($w - 1, 100) }
    } catch { $width = 80 }
    $pad = ' ' * $Indent
    $lines = New-Object 'System.Collections.Generic.List[string]'
    $line = ''
    foreach ($word in ($Text -split ' ')) {
        if ($line -and ($pad.Length + $line.Length + 1 + $word.Length) -gt $width) {
            $lines.Add($pad + $line)
            $pad = ' ' * ($Indent + $Hanging)
            $line = $word
        } elseif ($line) {
            $line = $line + ' ' + $word
        } else {
            $line = $word
        }
    }
    if ($line) { $lines.Add($pad + $line) }
    foreach ($l in $lines) {
        if ($Color) { Write-Host $l -ForegroundColor $Color } else { Write-Host $l }
    }
}

function Write-Warn([string]$Text) { Write-Text $Text 'Yellow' }

function Write-Step([int]$Number, [string]$Text) {
    Write-Host ''
    Write-Host ('Step {0} of {1}: {2}' -f $Number, $StepCount, $Text) -ForegroundColor Cyan
}

function Stop-Install([string]$Text) {
    Write-Host ''
    Write-Text $Text 'Red'
    Write-Host 'The installer has stopped. You can run it again at any time.'
    exit 1
}

function Show-Output([string[]]$Lines) {
    # What a program printed, indented, without the date and level that FOLIO-Exchange puts on log lines.
    foreach ($line in $Lines) {
        Write-Host ('    ' + ($line -replace '^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d [A-Z]+\s+', ''))
    }
}

# ---------------------------------------------------------------- questions

function Read-Answer([string]$Prompt) {
    if ($script:InputClosed) { return $null }
    try {
        $answer = Read-Host $Prompt
    } catch [System.Management.Automation.PSInvalidOperationException], [System.Management.Automation.Host.HostException] {
        $answer = $null      # PowerShell was started with -NonInteractive, as deployment tools do: nobody can answer
    }
    if ($null -eq $answer) {
        # No more input (for example, input from a file): text questions take their suggestion, and yes/no
        # questions are answered no.
        $script:InputClosed = $true
        return $null
    }
    return $answer.Trim()
}

function Read-Text([string]$Question, [string]$Default) {
    if ($Yes) {
        Write-Host ('{0} [{1}]: {1}' -f $Question, $Default)
        return $Default
    }
    $answer = Read-Answer ('{0} [{1}]' -f $Question, $Default)
    if ($script:InputClosed) {
        Write-Host ('{0} [{1}]: {1} (no answer was given)' -f $Question, $Default)
        return $Default
    }
    if ([string]::IsNullOrEmpty($answer)) { return $Default }
    return $answer
}

function Read-YesNo([string]$Question, [bool]$Default) {
    $hint = 'y/N'
    $word = 'no'
    if ($Default) { $hint = 'Y/n'; $word = 'yes' }
    if ($Yes) {
        Write-Host ('{0} [{1}]: {2}' -f $Question, $hint, $word)
        return $Default
    }
    while ($true) {
        $answer = Read-Answer ('{0} [{1}]' -f $Question, $hint)
        if ($script:InputClosed) {
            # Nobody can answer: do nothing that needs a yes (only -Yes stands in for one).
            Write-Host ('{0} [{1}]: no (no answer was given)' -f $Question, $hint)
            return $false
        }
        if ([string]::IsNullOrEmpty($answer)) { return $Default }
        if ($answer -match '^(y|yes)$') { return $true }
        if ($answer -match '^(n|no)$') { return $false }
        Write-Host 'Please answer y for yes or n for no, or press Enter for the suggestion.'
    }
}

function Test-CanAsk { return (-not $Yes) -and (-not $script:InputClosed) }

# ---------------------------------------------------------------- running programs

function ConvertTo-Line($Item) {
    $text = "$Item"
    # An empty line on stderr turns into this placeholder in PowerShell.
    if ($Item -is [System.Management.Automation.ErrorRecord] -and $text -eq 'System.Management.Automation.RemoteException') {
        return ''
    }
    return $text
}

function Invoke-Captured([string]$FilePath, [string[]]$ArgumentList) {
    # Runs a program and collects what it prints, to show only if needed. Never use it for a program that
    # asks the person something: its questions would not be seen.
    $ErrorActionPreference = 'Continue'      # Windows PowerShell 5.1 turns lines on stderr into errors
    $global:LASTEXITCODE = -1
    $all = @(& $FilePath @ArgumentList 2>&1)
    $code = $global:LASTEXITCODE
    $text = New-Object 'System.Collections.Generic.List[string]'
    $out = New-Object 'System.Collections.Generic.List[string]'
    foreach ($item in $all) {
        $line = ConvertTo-Line $item
        $text.Add($line)
        if ($item -isnot [System.Management.Automation.ErrorRecord]) { $out.Add($line) }
    }
    return [pscustomobject]@{ ExitCode = $code; Output = $text.ToArray(); StdOut = $out.ToArray() }
}

function Invoke-Interactive([string]$FilePath, [string[]]$ArgumentList) {
    # Runs a program in this window, so it can ask its own questions. Call it as a statement, never as
    # $x = Invoke-Interactive ..., and read the exit code from $LASTEXITCODE afterwards.
    $ErrorActionPreference = 'Continue'
    $global:LASTEXITCODE = -1
    & $FilePath @ArgumentList
}

function Format-Arg([string]$Value, [bool]$AlwaysQuote = $false) {
    if (-not $AlwaysQuote -and $Value -match '^[A-Za-z0-9_.:\\/=-]+$') { return $Value }
    if ($Value -match '[$`]') { return "'" + $Value.Replace("'", "''") + "'" }
    return '"' + $Value + '"'
}

function Format-Command([string]$FilePath, [string[]]$Arguments) {
    # The command as it is typed in PowerShell, for the person to run later.
    $parts = @('&', (Format-Arg $FilePath $true))
    foreach ($a in $Arguments) { $parts += (Format-Arg $a) }
    return ($parts -join ' ')
}

# ---------------------------------------------------------------- paths

function ConvertTo-FullPath([string]$Path) {
    # A full file-system path without a trailing backslash, or $null if the text is not a usable path.
    if ([string]::IsNullOrWhiteSpace($Path)) { return $null }
    $p = $Path.Trim()
    if ($p.Length -ge 2 -and $p.StartsWith('"') -and $p.EndsWith('"')) { $p = $p.Substring(1, $p.Length - 2).Trim() }
    $p = [Environment]::ExpandEnvironmentVariables($p)
    if (-not $p -or $p -match '[<>"|?*]') { return $null }
    try {
        $provider = $null
        $drive = $null
        $resolved = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($p, [ref]$provider, [ref]$drive)
        if ($provider.Name -ne 'FileSystem') { return $null }
        $full = [System.IO.Path]::GetFullPath($resolved)
    } catch {
        return $null
    }
    $root = [System.IO.Path]::GetPathRoot($full)
    if (-not $root) { return $null }
    if ($full.Substring($root.Length).Contains(':')) { return $null }
    if ($full.Length -gt $root.Length) { $full = $full.TrimEnd('\', '/') }
    return $full
}

function ConvertTo-FileUrl([string]$Path) {
    # file:///C:/Some%20Folder/x.zip, the form pip accepts after "folio-exchange @".
    return ([System.Uri]$Path).AbsoluteUri
}

function Test-Synced([string]$Path) {
    return ($Path -match 'OneDrive|Dropbox')
}

# ---------------------------------------------------------------- the copy being installed

function Get-Constraints([string]$Source, [string]$Into) {
    # The constraints.txt that comes with this copy of FOLIO-Exchange (the dependency versions it was tested
    # with), copied into the folder $Into, or $null if it has none. $Source is a folder or a zip archive, such
    # as GitHub's, where the file sits in the archive's one top folder.
    if (Test-Path -LiteralPath $Source -PathType Container) {
        $file = Join-Path $Source 'constraints.txt'
        if (Test-Path -LiteralPath $file -PathType Leaf) { return $file }
        return $null
    }
    if ($Source -notmatch '\.zip$') { return $null }
    try {
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $zip = [System.IO.Compression.ZipFile]::OpenRead($Source)
    } catch {
        return $null
    }
    try {
        $entry = $zip.Entries | Where-Object { $_.FullName -match '^([^/]+/)?constraints\.txt$' } | Select-Object -First 1
        if (-not $entry) { return $null }
        [void][System.IO.Directory]::CreateDirectory($Into)
        $file = Join-Path $Into 'constraints.txt'
        [System.IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $file, $true)
        return $file
    } catch {
        return $null
    } finally {
        $zip.Dispose()
    }
}

function Get-ZipCommit([string]$Path) {
    # The git commit of a zip archive made by GitHub (or git archive), which records it as the archive's
    # comment; $null for any other file.
    if ($Path -notmatch '\.zip$' -or -not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    try {
        $stream = [System.IO.File]::OpenRead($Path)
        try {
            $length = [int][Math]::Min($stream.Length, 65557)     # the end record and the longest comment
            [void]$stream.Seek(-$length, [System.IO.SeekOrigin]::End)
            $bytes = New-Object byte[] $length
            $read = 0
            while ($read -lt $length) {
                $n = $stream.Read($bytes, $read, $length - $read)
                if ($n -le 0) { break }
                $read += $n
            }
        } finally {
            $stream.Dispose()
        }
        for ($i = $length - 22; $i -ge 0; $i--) {
            if ($bytes[$i] -eq 0x50 -and $bytes[$i + 1] -eq 0x4b -and $bytes[$i + 2] -eq 0x05 -and $bytes[$i + 3] -eq 0x06) {
                $size = [int]$bytes[$i + 20] + 256 * [int]$bytes[$i + 21]
                if ($i + 22 + $size -gt $length) { return $null }
                $comment = [System.Text.Encoding]::ASCII.GetString($bytes, $i + 22, $size).Trim()
                if ($comment -match '^[0-9a-f]{40}$') { return $comment }
                return $null
            }
        }
    } catch {
        return $null
    }
    return $null
}

# ---------------------------------------------------------------- Python

function Get-PythonInfo([string]$FilePath, [string[]]$PrefixArgs) {
    $arguments = @()
    if ($PrefixArgs) { $arguments += $PrefixArgs }
    # -X utf8: print in UTF-8, which is how the output is read (a path such as C:\Users\Zoe-with-diaeresis\...)
    $arguments += @('-I', '-X', 'utf8', '-c', 'import sys;print(sys.version_info[0]);print(sys.version_info[1]);print(sys.executable)')
    try {
        $r = Invoke-Captured $FilePath $arguments
    } catch {
        return $null
    }
    if ($r.ExitCode -ne 0) { return $null }
    $lines = @($r.StdOut | Where-Object { $_.Trim() -ne '' })
    $n = $lines.Count
    if ($n -lt 3) { return $null }
    $major = 0
    $minor = 0
    if (-not [int]::TryParse($lines[$n - 3].Trim(), [ref]$major)) { return $null }
    if (-not [int]::TryParse($lines[$n - 2].Trim(), [ref]$minor)) { return $null }
    $exe = $lines[$n - 1].Trim()
    # Run Python by its own path from now on. Should that path not have come back intact, run it the way that
    # just worked instead.
    $runPath = $exe
    $runPrefix = @()
    if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) {
        $runPath = $FilePath
        if ($PrefixArgs) { $runPrefix = $PrefixArgs }
    }
    return [pscustomobject]@{
        Version    = '{0}.{1}' -f $major, $minor
        Executable = $exe
        RunPath    = $runPath
        RunPrefix  = $runPrefix
        NewEnough  = ($major -gt 3) -or ($major -eq 3 -and $minor -ge 10)
        FromStore  = ($exe -match '\\WindowsApps\\')
    }
}

function Find-Python {
    # Tries the Python launcher (py -3), then python, then the usual install folders.
    $candidates = New-Object 'System.Collections.Generic.List[object]'
    foreach ($name in @('py', 'python')) {
        $cmd = Get-Command $name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($cmd) {
            $prefix = @()
            if ($name -eq 'py') { $prefix = @('-3') }
            $candidates.Add([pscustomobject]@{ Path = $cmd.Path; Prefix = $prefix })
        }
    }
    $local = [Environment]::GetFolderPath('LocalApplicationData')
    foreach ($launcher in @((Join-Path $local 'Programs\Python\Launcher\py.exe'), (Join-Path $env:SystemRoot 'py.exe'))) {
        if (Test-Path -LiteralPath $launcher -PathType Leaf) {
            $candidates.Add([pscustomobject]@{ Path = $launcher; Prefix = @('-3') })
        }
    }
    $places = @(
        @((Join-Path $local 'Programs\Python'), 'Python3*'),
        @($env:ProgramFiles, 'Python3*'),
        @((Join-Path $local 'Python'), 'pythoncore-3*')
    )
    foreach ($place in $places) {
        $folder = $place[0]
        if (-not $folder -or -not (Test-Path -LiteralPath $folder -PathType Container)) { continue }
        $found = @(Get-ChildItem -LiteralPath $folder -Directory -Filter $place[1] -ErrorAction SilentlyContinue |
                   Sort-Object Name -Descending)
        foreach ($dir in $found) {
            $exe = Join-Path $dir.FullName 'python.exe'
            if (Test-Path -LiteralPath $exe -PathType Leaf) {
                $candidates.Add([pscustomobject]@{ Path = $exe; Prefix = @() })
            }
        }
    }

    $seen = @{}
    $notes = New-Object 'System.Collections.Generic.List[string]'
    foreach ($candidate in $candidates) {
        $info = Get-PythonInfo $candidate.Path $candidate.Prefix
        if (-not $info) { continue }
        $key = $info.Executable.ToLowerInvariant()
        if ($seen.ContainsKey($key)) { continue }
        $seen[$key] = $true
        if (-not $info.NewEnough) {
            $notes.Add(('Python {0} at {1} is too old.' -f $info.Version, $info.Executable))
            continue
        }
        if ($info.FromStore) {
            $notes.Add(('Python {0} from the Microsoft Store cannot be used: Windows keeps the files it makes in a hidden private folder.' -f $info.Version))
            continue
        }
        return [pscustomobject]@{ Python = $info; Notes = $notes.ToArray() }
    }
    return [pscustomobject]@{ Python = $null; Notes = $notes.ToArray() }
}

# ---------------------------------------------------------------- choices

function Select-InstallDir([string]$Default) {
    $given = $InstallDir
    while ($true) {
        if ($given) {
            $raw = $given
            $given = $null
        } else {
            $raw = Read-Text 'Install folder' $Default
        }
        $path = ConvertTo-FullPath $raw
        $problem = $null
        if (-not $path) {
            $problem = ('"{0}" is not a folder path that can be used here.' -f $raw)
        } elseif (Test-Path -LiteralPath $path -PathType Leaf) {
            $problem = ('{0} is a file, not a folder.' -f $path)
        }
        if ($problem) {
            if (-not (Test-CanAsk)) { Stop-Install $problem }
            Write-Warn $problem
            continue
        }
        if (Test-Synced $path) {
            Write-Warn ('{0} looks like part of OneDrive or Dropbox. Syncing breaks the files FOLIO-Exchange keeps while it works (its cache and lock files), so a folder outside them, such as the suggested one, is much better.' -f $path)
            if (-not (Read-YesNo 'Use this folder anyway?' $false)) {
                if (-not (Test-CanAsk)) { Stop-Install 'Choose an install folder outside OneDrive and Dropbox, then run the installer again.' }
                continue
            }
        }
        if ($path.Length -gt 100) {
            Write-Warn 'This folder path is long. Windows may refuse some of the program files under it; a shorter one, such as C:\FOLIO-Exchange, avoids that.'
        }
        return $path
    }
}

function Get-SiteNameProblem([string]$Name) {
    if ($Name -notmatch '^[A-Za-z0-9][A-Za-z0-9-]*$') {
        return 'Use only letters, digits and hyphens, starting with a letter or digit, for example mylibrary or main-campus.'
    }
    if ($Name.Length -gt 40) { return 'Keep the name to 40 characters or fewer.' }
    if ($Name -eq 'program') { return 'The name "program" is used for the program folder. Choose another name.' }
    if ($Name -match '^(CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])$') {
        return ('Windows does not allow a folder called {0}. Choose another name.' -f $Name)
    }
    return $null
}

function Select-SiteName([string]$Root) {
    $given = $SiteName
    while ($true) {
        if ($given) {
            $name = $given.Trim()
            $given = $null
        } else {
            $name = Read-Text 'A short name for this library''s site (letters, digits and hyphens)' 'mylibrary'
        }
        $problem = Get-SiteNameProblem $name
        if (-not $problem -and (Test-Path -LiteralPath (Join-Path $Root $name) -PathType Leaf)) {
            $problem = ('{0} is a file, so it cannot be the site folder. Choose another name.' -f (Join-Path $Root $name))
        }
        if ($problem) {
            if (-not (Test-CanAsk)) { Stop-Install ('The site name "{0}" cannot be used. {1}' -f $name, $problem) }
            Write-Warn $problem
            continue
        }
        return $name
    }
}

# ---------------------------------------------------------------- other checks

function Get-RunningCopies([string]$Folder) {
    # FOLIO-Exchange processes started from this program folder: the page, or a scheduled run.
    $prefix = $Folder.TrimEnd('\') + '\'
    foreach ($process in @(Get-Process -Name 'folio-exchange', 'python', 'pythonw' -ErrorAction SilentlyContinue)) {
        $file = $null
        try { $file = $process.Path } catch { $file = $null }
        if ($file -and $file.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) { $process }
    }
}

function Get-TaskAction([string]$TaskName) {
    # The program and arguments of an existing Windows scheduled task, or $null if there is no such task (or
    # it cannot be looked up, in which case the person is simply asked). Read through the ScheduledTasks
    # module that comes with Windows, so paths arrive intact rather than as text in a console code page.
    try {
        $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue | Select-Object -First 1
    } catch {
        return $null
    }
    if (-not $task) { return $null }
    $command = ''
    $arguments = ''
    try {
        $action = @($task.Actions)[0]
        if ($action.Execute) { $command = [Environment]::ExpandEnvironmentVariables(([string]$action.Execute).Trim().Trim('"')) }
        if ($action.Arguments) { $arguments = [string]$action.Arguments }
    } catch {
        $command = ''
    }
    # Tasks registered by older versions kept Task Scheduler's defaults: no start on battery power.
    $battery = $false
    try {
        $battery = [bool]($task.Settings.DisallowStartIfOnBatteries -or $task.Settings.StopIfGoingOnBatteries)
    } catch {
        $battery = $false
    }
    return [pscustomobject]@{ Command = $command; Arguments = $arguments; Battery = $battery }
}

# ---------------------------------------------------------------- the installer

# Read what programs print as UTF-8 (Python is run with -X utf8, and FOLIO-Exchange prints UTF-8), so that
# paths such as C:\Users\Jose-with-accent\Desktop arrive intact. The old settings come back at the end.
$Utf8 = New-Object System.Text.UTF8Encoding $false
$savedConsoleEncoding = $null
$savedOutputEncoding = $OutputEncoding
try {
    try {
        $savedConsoleEncoding = [Console]::OutputEncoding
        [Console]::OutputEncoding = $Utf8
    } catch {
        $savedConsoleEncoding = $null
    }
    $OutputEncoding = $Utf8

    if ([System.Environment]::OSVersion.Platform -ne [System.PlatformID]::Win32NT) {
        Write-Host 'This installer is for Windows. On other systems, follow the install steps in the FOLIO-Exchange README.'
        exit 1
    }

    Write-Host ''
    Write-Host 'FOLIO-Exchange installer' -ForegroundColor Cyan
    Write-Text 'This installs FOLIO-Exchange, or updates it if it is already installed. Where a suggestion is shown in [brackets], press Enter to accept it or type your own answer. Press Ctrl+C to stop at any time.'

    $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object System.Security.Principal.WindowsPrincipal($identity)
    if ($principal.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Write-Warn ('This window runs as administrator, which is not needed. FOLIO-Exchange is installed for the Windows user {0}, so run the installer signed in as the person who will use it.' -f $env:USERNAME)
    }

    # Check the options first, so a mistake stops the installer before it changes anything.
    $sourceUrl = $null       # a URL that pip downloads
    $sourcePath = $null      # a local folder or archive
    if ($Source -and $Ref) { Stop-Install 'Give -Source or -Ref, not both.' }
    if ($Ref) {
        if ($Ref -notmatch '^[A-Za-z0-9][A-Za-z0-9._/-]*$') { Stop-Install ('-Ref: "{0}" is not a branch or tag name.' -f $Ref) }
        $sourceUrl = '{0}/archive/{1}.zip' -f $RepoUrl, $Ref
    } elseif ($Source) {
        $given = $Source.Trim()
        if ($given -match '^file:') {
            try { $given = ([System.Uri]$given).LocalPath } catch { Stop-Install ('-Source: "{0}" is not a usable file URL.' -f $Source) }
        }
        if ($given -match '^[A-Za-z][A-Za-z0-9+.-]+://') {
            $sourceUrl = $given
        } else {
            $sourcePath = ConvertTo-FullPath $given
            if (-not $sourcePath -or -not (Test-Path -LiteralPath $sourcePath)) { Stop-Install ('-Source: {0} was not found.' -f $given) }
            if ((Test-Path -LiteralPath $sourcePath -PathType Container) -and
                -not (Test-Path -LiteralPath (Join-Path $sourcePath 'pyproject.toml') -PathType Leaf)) {
                Stop-Install ('-Source: {0} has no pyproject.toml, so it is not a copy of FOLIO-Exchange.' -f $sourcePath)
            }
        }
    } else {
        $sourceUrl = $DefaultSource
    }
    $launcherDir = $null
    if ($LauncherFolder) {
        $launcherDir = ConvertTo-FullPath $LauncherFolder
        if (-not $launcherDir) { Stop-Install ('-LauncherFolder: "{0}" is not a folder path that can be used here.' -f $LauncherFolder) }
        if (Test-Path -LiteralPath $launcherDir -PathType Leaf) { Stop-Install ('-LauncherFolder: {0} is a file, not a folder.' -f $launcherDir) }
    }

    # ------------------------------------------------------------ 1. Python
    Write-Step 1 'Checking for Python'
    $search = Find-Python
    $python = $search.Python
    if (-not $python) {
        Write-Warn 'FOLIO-Exchange needs Python 3.10 or newer, and none was found that it can use.'
        foreach ($note in $search.Notes) { Write-Text $note '' 2 2 }
        $winget = Get-Command winget -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($winget) {
            Write-Text 'Windows can install Python 3.12 for you with winget, the Windows Package Manager. It downloads Python from python.org.'
            if (Read-YesNo 'Install Python 3.12 now?' $true) {
                # --scope user: installed for this Windows user only, so no administrator rights are needed
                $wingetArgs = @('install', '-e', '--id', $WingetPython, '--scope', 'user')
                Write-Host ''
                Write-Host ('Running: winget {0}' -f ($wingetArgs -join ' '))
                Invoke-Interactive $winget.Path $wingetArgs
                $code = $global:LASTEXITCODE
                Write-Host ''
                if ($code -ne 0) { Write-Warn ('winget ended with code {0}. Checking for Python again anyway.' -f $code) }
                # A new install changes PATH for new windows only; pick up the change here too.
                $env:Path = $env:Path + ';' + [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
                            [Environment]::GetEnvironmentVariable('Path', 'User')
                $search = Find-Python
                $python = $search.Python
            }
        }
        if (-not $python) {
            Stop-Install 'Install Python 3.10 or newer from https://www.python.org/downloads/windows/, then run this installer again.'
        }
    }
    Write-Host ('Found Python {0}: {1}' -f $python.Version, $python.Executable)

    # ------------------------------------------------------------ 2. folders
    Write-Step 2 'Choosing the folders'
    $defaultInstall = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'FOLIO-Exchange'
    $installRoot = Select-InstallDir $defaultInstall
    $siteName = Select-SiteName $installRoot
    $sitePath = Join-Path $installRoot $siteName
    $venv = Join-Path $installRoot 'program'
    $venvPython = Join-Path $venv 'Scripts\python.exe'
    $exe = Join-Path $venv 'Scripts\folio-exchange.exe'
    $isUpdate = Test-Path -LiteralPath (Join-Path $venv 'pyvenv.cfg') -PathType Leaf
    Write-Host ''
    Write-Host ('The program goes in:             {0}' -f $venv)
    Write-Host ('This library''s settings go in:   {0}' -f $sitePath)
    if ($isUpdate) { Write-Host 'FOLIO-Exchange is already installed there, so it will be updated.' }
    if (Test-Path -LiteralPath (Join-Path $sitePath 'site.yaml') -PathType Leaf) {
        Write-Host ('The settings for {0} are already there. They are kept.' -f $siteName)
    }

    # ------------------------------------------------------------ 3. program
    if ($isUpdate) { Write-Step 3 'Updating FOLIO-Exchange' } else { Write-Step 3 'Installing FOLIO-Exchange' }

    if (-not $isUpdate -and (Test-Path -LiteralPath $venv)) {
        $other = @(Get-ChildItem -LiteralPath $venv -Force -ErrorAction SilentlyContinue)
        if ($other.Count -gt 0) {
            Stop-Install ('{0} already holds other files, so the installer leaves it alone. Move them, or choose another install folder.' -f $venv)
        }
    }

    $running = @(Get-RunningCopies $venv)
    if ($running.Count -gt 0) {
        Write-Warn 'FOLIO-Exchange is running from this folder right now: the page, or a scheduled run. Updating it while it runs can fail.'
        Write-Text 'Close the black window that keeps the page open, and wait for any run to finish. Then continue.'
        if (-not (Read-YesNo 'Continue with the update now?' $false)) {
            Stop-Install 'Run the installer again once FOLIO-Exchange has stopped.'
        }
    }

    $makeVenv = $true
    $clear = $false
    if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
        $current = Get-PythonInfo $venvPython @()
        if ($current -and $current.NewEnough) {
            $makeVenv = $false
        } else {
            $clear = $true
            Write-Warn 'The program folder no longer works, probably because the Python it was made with has been removed. It is made again; this library''s settings are not in it and are kept.'
        }
    } elseif ($isUpdate) {
        $clear = $true
    }
    if ($makeVenv) {
        Write-Host ('Making the program folder with Python {0}.' -f $python.Version)
        [void][System.IO.Directory]::CreateDirectory($installRoot)
        $venvArgs = @()
        if ($python.RunPrefix) { $venvArgs += $python.RunPrefix }
        $venvArgs += @('-I', '-X', 'utf8', '-m', 'venv')
        if ($clear) { $venvArgs += '--clear' }
        $venvArgs += $venv
        $r = Invoke-Captured $python.RunPath $venvArgs
        if ($r.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
            Show-Output $r.Output
            Stop-Install ('The program folder {0} could not be made with Python {1}.' -f $venv, $python.Version)
        }
    }

    $pip = @('-I', '-X', 'utf8', '-m', 'pip')
    $pipQuiet = @('--no-input', '--disable-pip-version-check')
    if ($sourceUrl) {
        Write-Host ('Getting FOLIO-Exchange from {0}' -f $sourceUrl)
    } else {
        Write-Host ('Getting FOLIO-Exchange from {0}' -f $sourcePath)
    }
    Write-Text 'Downloading and installing it and the parts it needs. This can take a few minutes, and nothing is shown until it is done.'
    $download = $null
    $constraintsDir = $null
    $commit = $null
    try {
        $archive = $sourcePath
        if ($sourceUrl) {
            # pip does not reinstall from a web address it has installed from before when the version number
            # is unchanged, so download the archive first: pip always reinstalls from a local archive.
            $download = Join-Path ([System.IO.Path]::GetTempPath()) ('folio-exchange-install-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
            [void][System.IO.Directory]::CreateDirectory($download)
            $r = Invoke-Captured $venvPython ($pip + @('download') + $pipQuiet +
                                              @('--no-deps', '--no-cache-dir', '--dest', $download, ('folio-exchange @ ' + $sourceUrl)))
            if ($r.ExitCode -ne 0) {
                Show-Output $r.Output
                $hint = 'Check that this computer can reach github.com and pypi.org (a firewall or proxy may block them)'
                if ($Ref) { $hint = $hint + (', and that the branch or tag "{0}" exists' -f $Ref) }
                Stop-Install ('FOLIO-Exchange could not be downloaded from {0}. {1}, then run the installer again.' -f $sourceUrl, $hint)
            }
            $files = @(Get-ChildItem -LiteralPath $download -File)
            if ($files.Count -ne 1) {
                Show-Output $r.Output
                Stop-Install 'The download did not give the one file that was expected.'
            }
            $archive = $files[0].FullName
        }
        $commit = Get-ZipCommit $archive
        if ($commit) { Write-Host ('This is commit {0}.' -f $commit.Substring(0, 12)) }
        # Install the parts it needs at the versions this copy was tested with (its constraints.txt).
        $constraintsDir = Join-Path ([System.IO.Path]::GetTempPath()) ('folio-exchange-constraints-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
        $constraints = Get-Constraints $archive $constraintsDir
        $pinned = @()
        if ($constraints) {
            $pinned = @('-c', $constraints)
        } else {
            Write-Warn 'This copy of FOLIO-Exchange has no constraints.txt, so the newest versions of the parts it needs are installed rather than the tested ones.'
        }
        $r = Invoke-Captured $venvPython ($pip + @('install') + $pipQuiet + $pinned + @('--upgrade', ('folio-exchange @ ' + (ConvertTo-FileUrl $archive))))
        if ($r.ExitCode -ne 0) {
            Show-Output $r.Output
            Stop-Install 'FOLIO-Exchange could not be installed; the messages above say why. If they mention a connection problem, check that this computer can reach pypi.org and files.pythonhosted.org. If they mention a path that is too long, choose a shorter install folder. If FOLIO-Exchange was running, close it and try again.'
        }
    } finally {
        foreach ($temp in @($download, $constraintsDir)) {
            if ($temp -and (Test-Path -LiteralPath $temp)) {
                Remove-Item -LiteralPath $temp -Recurse -Force -ErrorAction SilentlyContinue
            }
        }
    }

    if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) { Stop-Install ('The install finished, but {0} is missing.' -f $exe) }
    $r = Invoke-Captured $exe @('--version')
    if ($r.ExitCode -ne 0) {
        Show-Output $r.Output
        Stop-Install 'FOLIO-Exchange was installed, but it does not start.'
    }
    $version = @($r.StdOut | Where-Object { $_.Trim() -ne '' } | Select-Object -Last 1)
    if ($version.Count -eq 1) { $version = $version[0].Trim() } else { $version = 'folio-exchange (version not known)' }
    if ($commit) { $version = '{0}, commit {1}' -f $version, $commit.Substring(0, 12) }
    if ($isUpdate) { Write-Host ('Updated: {0}' -f $version) } else { Write-Host ('Installed: {0}' -f $version) }

    # ------------------------------------------------------------ 4. launcher
    Write-Step 4 'Making the launcher'
    $launcherArgs = @('-s', $sitePath, 'launcher')
    if ($launcherDir) {
        [void][System.IO.Directory]::CreateDirectory($launcherDir)
        $launcherArgs += @('--folder', $launcherDir)
    }
    $launcherPath = $null
    $r = Invoke-Captured $exe $launcherArgs
    if ($r.ExitCode -eq 0) {
        foreach ($line in $r.StdOut) {
            if ($line -match '^Made (.+\.cmd)\s*$') {
                $made = $Matches[1].Trim()
                if (Test-Path -LiteralPath $made -PathType Leaf) { $launcherPath = $made }
            }
        }
        if (-not $launcherPath) {
            # Not reported, or not readable: look where it should be.
            $where = $launcherDir
            if (-not $where) { $where = [Environment]::GetFolderPath('Desktop') }
            $guess = Join-Path $where ('FOLIO-Exchange ({0}).cmd' -f $siteName)
            if (Test-Path -LiteralPath $guess -PathType Leaf) { $launcherPath = $guess }
        }
        if ($launcherPath) {
            Write-Host ('Made {0}' -f $launcherPath)
            Write-Host 'Double-click it to open the FOLIO-Exchange page.'
        } else {
            Write-Host 'The launcher was made. FOLIO-Exchange said:'
            Show-Output $r.Output
        }
    } else {
        $later = Format-Command $exe $launcherArgs
        Write-Warn 'The launcher could not be made. FOLIO-Exchange said:'
        Show-Output $r.Output
        Write-Host 'To make it later, run this in PowerShell:'
        Write-Host ('  {0}' -f $later)
        $script:ToDo.Add(('Make the launcher. In PowerShell: {0}' -f $later))
    }

    # ------------------------------------------------------------ 5. scheduled runs
    Write-Step 5 'Scheduled runs'
    $taskName = 'FOLIO-Exchange ({0})' -f $siteName
    $registerArgs = @('-s', $sitePath, 'schedule', '--register')
    $registerLater = Format-Command $exe $registerArgs
    $scheduleState = 'not set up'
    if ($NoSchedule) {
        Write-Host 'Skipped, as asked (-NoSchedule). To set them up later, run this in PowerShell:'
        Write-Host ('  {0}' -f $registerLater)
    } else {
        $task = Get-TaskAction $taskName
        $same = $false
        if ($task -and $task.Command -and $task.Command -eq $exe -and
            $task.Arguments.IndexOf($sitePath, [System.StringComparison]::OrdinalIgnoreCase) -ge 0) {
            $same = $true
        }
        if ($same -and $task.Battery) {
            Write-Text ('The Windows task "{0}" was set up by an older version, which does not start it on battery power. Setting up scheduled runs again fixes that.' -f $taskName)
        }
        if ($same -and -not $task.Battery) {
            Write-Text ('Scheduled runs are already set up: the Windows task "{0}" starts this copy of FOLIO-Exchange every hour.' -f $taskName)
            $scheduleState = 'already set up'
        } else {
            if ($task -and -not $same) {
                Write-Warn ('A Windows task called "{0}" already exists, but it starts another copy of FOLIO-Exchange or another site. Setting up scheduled runs replaces it.' -f $taskName)
            }
            Write-Text 'Windows can start FOLIO-Exchange every hour to run the feeds that are due. For this, Windows asks for your Windows password, so that the runs also happen when nobody is signed in.'
            if (Read-YesNo 'Set up scheduled runs now?' $true) {
                Write-Text 'When asked, type the password you use to sign in to Windows (it is not shown) and press Enter. If you sign in with a PIN, use the password of your Microsoft or work account instead. Windows Task Scheduler keeps it; this installer does not see it.'
                Write-Host ''
                [void][System.IO.Directory]::CreateDirectory($sitePath)    # normally made by the launcher already
                Invoke-Interactive $exe $registerArgs
                $code = $global:LASTEXITCODE
                Write-Host ''
                if ($code -eq 0) {
                    $scheduleState = 'set up'
                    Write-Host 'Scheduled runs are set up.'
                } else {
                    $scheduleState = 'not set up (it did not work)'
                    Write-Warn 'Scheduled runs could not be set up. To try again later, signed in to Windows as yourself, run this in PowerShell:'
                    Write-Host ('  {0}' -f $registerLater)
                    $script:ToDo.Add(('Set up scheduled runs. In PowerShell: {0}' -f $registerLater))
                }
            } else {
                Write-Host 'Not now. To set them up later, run this in PowerShell:'
                Write-Host ('  {0}' -f $registerLater)
            }
        }
    }

    # ------------------------------------------------------------ 6. the page
    Write-Step 6 'Opening the FOLIO-Exchange page'
    $nextSteps = {
        Write-Text 'On the page:'
        Write-Text '1. Connect to FOLIO: the API address, the tenant id, and a user name and password. Your FOLIO administrator or hosting provider can give you these.' '' 2 3
        Write-Text '2. Add the vendor feeds, such as collectionHQ, with their passwords.' '' 2 3
        Write-Text 'Passwords typed on the page are kept in Windows Credential Manager, never in files.'
    }
    if ($NoStart) {
        Write-Host 'Skipped, as asked (-NoStart).'
        if ($launcherPath) { Write-Text ('To open the page, double-click {0}.' -f [System.IO.Path]::GetFileName($launcherPath)) }
        & $nextSteps
    } elseif (-not $launcherPath) {
        $openLater = Format-Command $exe @('-s', $sitePath, 'ui')
        Write-Warn 'Without the launcher, the page is not opened from here. To open it, run this in PowerShell:'
        Write-Host ('  {0}' -f $openLater)
        & $nextSteps
    } else {
        $started = $false
        try {
            Start-Process -FilePath $launcherPath
            $started = $true
        } catch {
            try {
                Invoke-Item -LiteralPath $launcherPath
                $started = $true
            } catch {
                $started = $false
            }
        }
        if ($started) {
            Write-Text 'The page opens in your web browser in a moment, with a black window beside it. Keep the black window open while you use the page; closing it closes the page.'
            & $nextSteps
        } else {
            Write-Warn ('The page could not be opened from here. Double-click {0} to open it.' -f $launcherPath)
            $script:ToDo.Add(('Open the page: double-click {0}' -f $launcherPath))
        }
    }

    # ------------------------------------------------------------ summary
    Write-Host ''
    Write-Host '------------------------------------------------------------------------'
    if ($isUpdate) { Write-Host 'FOLIO-Exchange is updated.' -ForegroundColor Green } else { Write-Host 'FOLIO-Exchange is installed.' -ForegroundColor Green }
    Write-Host ('  Program:         {0} ({1})' -f $venv, $version)
    Write-Host ('  Library site:    {0}' -f $sitePath)
    if ($launcherPath) {
        Write-Host ('  Launcher:        {0}, in {1}' -f [System.IO.Path]::GetFileName($launcherPath), [System.IO.Path]::GetDirectoryName($launcherPath))
    } else {
        Write-Host '  Launcher:        not made yet'
    }
    Write-Host ('  Scheduled runs:  {0}' -f $scheduleState)
    Write-Host ''
    Write-Text 'To update FOLIO-Exchange later, run this installer again and give the same install folder and site name. The settings, passwords and run history are kept.'
    if ($script:ToDo.Count -gt 0) {
        Write-Host ''
        Write-Host 'Still to do:' -ForegroundColor Yellow
        foreach ($item in $script:ToDo) { Write-Host ('  - {0}' -f $item) }
        exit 2
    }
    exit 0
} catch {
    Write-Host ''
    Write-Host ('The installer stopped because of an unexpected problem: {0}' -f $_.Exception.Message) -ForegroundColor Red
    if ($_.InvocationInfo -and $_.InvocationInfo.ScriptLineNumber) {
        Write-Host ('(install.ps1, line {0})' -f $_.InvocationInfo.ScriptLineNumber)
    }
    exit 1
} finally {
    if ($savedConsoleEncoding) {
        try { [Console]::OutputEncoding = $savedConsoleEncoding } catch { $savedConsoleEncoding = $null }
    }
    $OutputEncoding = $savedOutputEncoding
}
