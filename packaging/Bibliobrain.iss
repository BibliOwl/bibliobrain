#ifndef SourceRoot
  #define SourceRoot ".."
#endif
#ifndef MyAppVersion
  ; Read the shared VERSION file so the installer never drifts from src/version.py.
  #define VersionFile FileOpen(AddBackslash(SourceRoot) + "VERSION")
  #define MyAppVersion Trim(FileRead(VersionFile))
  #expr FileClose(VersionFile)
#endif
#ifndef PayloadVersionFile
  #define PayloadVersionFile AddBackslash(SourceRoot) + "VERSION"
#endif

#ifdef PackagingSmoke
  #define InstallerAppId "{{F49C6DE4-7D27-49C8-81E7-C910C06F6510}"
  #define InstallerAppName "Bibliobrain Installer Smoke Test"
  #define InstallerOutputBaseFilename "Bibliobrain-Setup-Smoke"
#else
  #define InstallerAppId "{{8D9DBE54-77CD-4B79-B443-C42D54303ED7}"
  #define InstallerAppName "Bibliobrain"
  ; The version in the name tells which one a downloaded file installs.
  #define InstallerOutputBaseFilename "Bibliobrain-Setup-" + MyAppVersion
#endif

; The payload smoke test installs nothing on the machine: it only runs the
; post-install script when it supplies a stub (SmokeInstallScript).
#ifndef PackagingSmoke
  #define WithInstallScript
#endif
#ifdef SmokeInstallScript
  #define WithInstallScript
#endif

[Setup]
AppId={#InstallerAppId}
AppName={#InstallerAppName}
AppVersion={#MyAppVersion}
AppPublisher=Bibliobrain
DefaultDirName={localappdata}\Programs\Bibliobrain
DefaultGroupName=Bibliobrain
UninstallDisplayIcon={app}\static\logo_bibliobrain.ico
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename={#InstallerOutputBaseFilename}
SetupIconFile={#SourceRoot}\static\logo_bibliobrain.ico
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
DisableProgramGroupPage=yes
; No licence page: the AGPL needs no acceptance to receive or run the program
; (section 9). Its text is installed with the application and the summary page says so.

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"
Name: "fr"; MessagesFile: "compiler:Languages\French.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[InstallDelete]
; An update must not leave behind modules that the new version removed or
; renamed: Python would still import them. Only code folders are cleared; user
; data lives under %LOCALAPPDATA%\bibliobrain and .venv is reused by install.ps1.
Type: filesandordirs; Name: "{app}\src"
Type: filesandordirs; Name: "{app}\static"
Type: filesandordirs; Name: "{app}\wiki"

[Files]
Source: "{#SourceRoot}\wiki\*"; DestDir: "{app}\wiki"; Flags: ignoreversion recursesubdirs createallsubdirs
; Product allowlist. Never package the developer's working tree wholesale:
; it can contain local PDFs, benchmark excerpts, logs, screenshots and secrets.
Source: "{#SourceRoot}\app_api.py";              DestDir: "{app}"; Flags: ignoreversion
Source: "{#PayloadVersionFile}";                 DestDir: "{app}"; DestName: "VERSION"; Flags: ignoreversion
#ifdef SmokeInstallScript
Source: "{#SmokeInstallScript}";                 DestDir: "{app}"; DestName: "install.ps1"; Flags: ignoreversion
#else
Source: "{#SourceRoot}\install.ps1";             DestDir: "{app}"; Flags: ignoreversion
#endif
; The model page of the wizard runs install.ps1 -ProbeModelsFile before any
; file is installed, so it has to be available from the temporary folder.
Source: "{#SourceRoot}\install.ps1";             DestName: "install-probe.ps1"; Flags: dontcopy
Source: "{#SourceRoot}\Install-Bibliobrain.cmd"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceRoot}\requirements.txt";        DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceRoot}\requirements.lock";       DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceRoot}\pyproject.toml";          DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceRoot}\README.md";               DestDir: "{app}"; Flags: ignoreversion
; User guides are shipped from wiki/ above. Never package private docs/ files.
Source: "{#SourceRoot}\LICENSE";                 DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceRoot}\THIRD_PARTY_NOTICES.md";  DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceRoot}\THIRD_PARTY_LICENSES\*";  DestDir: "{app}\THIRD_PARTY_LICENSES"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#SourceRoot}\configuration.example.json"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceRoot}\src\*";                   DestDir: "{app}\src"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "__pycache__\*,*.pyc"
Source: "{#SourceRoot}\static\*";                DestDir: "{app}\static"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#SourceRoot}\scripts\*";               DestDir: "{app}\scripts"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "__pycache__\*,*.pyc"

; The smoke test must never touch the shortcuts of a real installation.
#ifndef PackagingSmoke
[Icons]
; Hidden PowerShell: the shortcut opens the application without a console.
Name: "{group}\Bibliobrain"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File ""{app}\scripts\bibliobrain-manager.ps1"" -Action start -ErrorDialog"; WorkingDir: "{app}"; IconFilename: "{app}\static\logo_bibliobrain.ico"
Name: "{autodesktop}\Bibliobrain"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File ""{app}\scripts\bibliobrain-manager.ps1"" -Action start -ErrorDialog"; WorkingDir: "{app}"; IconFilename: "{app}\static\logo_bibliobrain.ico"; Tasks: desktopicon
#endif

; install.ps1 is started from [Code] (RunInstallScript), not from [Run]: Setup
; has to know whether it succeeded.
#ifndef PackagingSmoke
[Run]
; Offered on the last page only: Setup has no console left to start it from.
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File ""{app}\scripts\bibliobrain-manager.ps1"" -Action start -ErrorDialog"; WorkingDir: "{app}"; Description: "{cm:LaunchBibliobrain}"; Flags: postinstall nowait skipifsilent runhidden; Check: InstallScriptSucceeded

[UninstallRun]
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\scripts\bibliobrain-manager.ps1"" -Action uninstall -Confirm -NativeInstaller"; WorkingDir: "{app}"; Flags: runhidden; RunOnceId: "BibliobrainRuntimeCleanup"
#endif

[UninstallDelete]
Type: filesandordirs; Name: "{app}\.venv"
Type: filesandordirs; Name: "{app}\.bibliobrain"
; Python leaves __pycache__ folders next to the code it ran, and install.ps1
; its log: without these lines the program folder survives the uninstall.
Type: filesandordirs; Name: "{app}\src"
Type: filesandordirs; Name: "{app}\scripts"
Type: filesandordirs; Name: "{app}\__pycache__"
Type: files; Name: "{app}\bibliobrain-install.log"
Type: files; Name: "{app}\run.bat"
Type: files; Name: "{app}\Bibliobrain.lnk"
Type: files; Name: "{app}\Stop-Bibliobrain.cmd"
Type: files; Name: "{app}\Repair-Bibliobrain.cmd"
Type: files; Name: "{app}\Update-Bibliobrain.cmd"
Type: files; Name: "{app}\Backup-Bibliobrain.cmd"
Type: files; Name: "{app}\Restore-Bibliobrain.cmd"
Type: files; Name: "{app}\Uninstall-Bibliobrain.cmd"
; Shortcut created by install.ps1 before Setup owned the shortcuts.
#ifndef PackagingSmoke
Type: files; Name: "{userdesktop}\Bibliobrain.lnk"
#endif

[CustomMessages]
en.ModeTitle=Type of installation
en.ModeSubtitle=How do you want to install Bibliobrain?
en.ModeHint=If you are not sure, keep the simple installation.
en.ModeEasy=Simple installation (recommended): Bibliobrain installs everything it needs, including the AI that suits this computer and the module that searches your documents. By choosing it, you agree to these downloads (several gigabytes).
en.ModeCustom=Custom installation: choose the folder, the LLM to download and the shortcuts yourself.
fr.ModeTitle=Type d'installation
fr.ModeSubtitle=Comment voulez-vous installer Bibliobrain ?
fr.ModeHint=Si vous hésitez, gardez l'installation simple.
fr.ModeEasy=Installation simple (conseillée) : Bibliobrain installe tout ce qu'il lui faut, dont l'IA qui convient à cet ordinateur et le module de recherche dans vos documents. En la choisissant, vous acceptez ces téléchargements (plusieurs gigaoctets).
fr.ModeCustom=Installation personnalisée : choisir vous-même le dossier, le LLM à télécharger et les raccourcis.
en.SummaryTitle=Before installation
en.SummarySubtitle=What Bibliobrain will install
en.SummaryHint=Bibliobrain's AI runs entirely on this computer: your documents are never sent anywhere. Here is what will happen.
en.SummaryBody=What the installation does on its own:%n• It installs Bibliobrain in the folder you chose.%n• It adds the tools Bibliobrain needs to work, if they are missing: Python, Ollama (the program that runs the AI) and a few Windows components. Tools you already have are kept.%n• It downloads a small module that sorts search results.%n%nWhat you choose on the next pages:%n• The AI that will answer your questions. We suggest the one that suits this computer.%n• The module that lets Bibliobrain search your documents.%nNeither of them is downloaded without your consent.%n%nLater, only if you ask for them:%n• Reading scanned documents, and the large AI for in-depth research. Bibliobrain asks for your permission before downloading them.%n%nWhat you need:%n• An Internet connection: several gigabytes are downloaded.%n• A few gigabytes of free disk space.%n• Some time: it depends mostly on the speed of your connection. You can do something else meanwhile.%n%nBibliobrain is free software under the GNU AGPL v3. The text of the license is in the installation folder.
fr.SummaryTitle=Avant l'installation
fr.SummarySubtitle=Ce que Bibliobrain va installer
fr.SummaryHint=L'IA de Bibliobrain fonctionne entièrement sur cet ordinateur : vos documents ne sont envoyés nulle part. Voici ce qui va se passer.
fr.SummaryBody=Ce que l'installation fait toute seule :%n• Elle installe Bibliobrain dans le dossier choisi.%n• Elle ajoute les outils dont Bibliobrain a besoin pour fonctionner, s'ils manquent : Python, Ollama (le programme qui fait tourner l'IA) et quelques composants Windows. Les outils déjà présents sont gardés.%n• Elle télécharge un petit module qui trie les résultats de recherche.%n%nCe que vous choisirez aux pages suivantes :%n• L'IA qui répondra à vos questions. Nous vous conseillons celle qui convient à cet ordinateur.%n• Le module qui permet à Bibliobrain de chercher dans vos documents.%nAucun des deux n'est téléchargé sans votre accord.%n%nPlus tard, seulement si vous le demandez :%n• La lecture des documents scannés et la grande IA de recherche approfondie. Bibliobrain vous demandera votre accord avant de les télécharger.%n%nÀ prévoir :%n• Une connexion Internet : plusieurs gigaoctets sont téléchargés.%n• Quelques gigaoctets libres sur le disque.%n• Du temps : cela dépend surtout de la vitesse de votre connexion. Vous pouvez faire autre chose pendant ce temps.%n%nBibliobrain est un logiciel libre, distribué sous licence GNU AGPL v3. Le texte de la licence se trouve dans le dossier d'installation.

; Sizes reach the page in GB (install.ps1 converts): the unit people know.
en.SizeUnit=GB
en.DecimalSeparator=.
fr.SizeUnit=Go
fr.DecimalSeparator=,
en.SizeUnitMB=MB
fr.SizeUnitMB=Mo
en.EtaUnderMinute=less than a minute
en.EtaMinutes=%1 min
en.EtaHours=%1 h %2 min
fr.EtaUnderMinute=moins d'une minute
fr.EtaMinutes=%1 min
fr.EtaHours=%1 h %2 min
en.ModelsTitle=AI models
en.ModelsSubtitle=Choose the AI that will answer your questions
en.ModelsIntro=Bibliobrain answers your questions with an AI (a "model") that runs on this computer. If unsure, keep the one that is selected: it suits this computer. You can change it later in Settings.
en.ModelsIntroNoPick=Bibliobrain answers your questions with an AI (a "model") that runs on this computer. None of the AIs we tested suits this computer well: you can try a smaller one, which is less reliable, or choose later in Settings.
en.ModelsAnalyzing=Looking at what this computer can do…
en.ModelsProbeFailed=The configuration of this computer could not be found. Which AI do you want to use? If unsure, keep the selected one: the smallest, it runs on most computers.
en.HardwareGpu=Your computer: %1 GB of memory and a graphics card suited to AI (%3 GB).
en.HardwareNoGpu=Your computer: %1 GB of memory, no graphics card suited to AI, so answers will take longer.
en.ModelsNoOllama=No AI is installed on this computer yet.
en.ModelsInstalledNone=No AI is installed on this computer yet.
en.ModelsInstalledCount=AIs already installed on this computer: %1 (marked “already installed”). Choosing one of them downloads nothing.
en.ChatNone=None for now (choose one later in Settings)
en.EasyBelowMinimum=This computer does not reach the minimum configuration to run an AI (8 GB of memory and 4 processor cores): Bibliobrain will not work properly with an AI on it, so none will be downloaded.%n%nYou can still install it and use a remote provider (an external API) to ask your questions, configured later in Settings. Your PDFs are prepared on this computer either way.%n%nOK to install anyway, Cancel to go back.
en.ModelsNote=The bigger an AI, the better its answers, but the more powerful the computer it needs. “A few mistakes” and “less reliable”: our tests found mistakes in the answers or in the sources they cite.
en.EmbeddingDownload=Also download the module that searches your documents (0.6 GB): required
en.EmbeddingInstalled=The module that searches your documents is already installed
en.FitGpu=fast on this computer
en.FitGpuCpu=slow on this computer
en.FitCpu=slow on this computer
en.FitTooBig=too heavy for this computer
en.FitUnknown=fit to this computer unknown
en.MarkRecommended=recommended
en.MarkInstalled=already installed
en.MarkCaveats=a few mistakes
en.MarkLimited=less reliable
en.MarkNotTested=not tested by us
fr.ModelsTitle=Modèles d'IA
fr.ModelsSubtitle=Choisissez l'IA qui répondra à vos questions
fr.ModelsIntro=Bibliobrain répond à vos questions grâce à une IA (un « modèle ») qui fonctionne sur cet ordinateur. Dans le doute, gardez celle qui est cochée : elle convient à cet ordinateur. Vous pourrez en changer plus tard dans les Réglages.
fr.ModelsIntroNoPick=Bibliobrain répond à vos questions grâce à une IA (un « modèle ») qui fonctionne sur cet ordinateur. Aucune des IA que nous avons testées ne lui convient vraiment : vous pouvez en essayer une plus petite, moins fiable, ou choisir plus tard dans les Réglages.
fr.ModelsAnalyzing=Analyse de votre ordinateur…
fr.ModelsProbeFailed=La configuration de cet ordinateur n'a pas été trouvée. Quelle IA voulez-vous utiliser ? Dans le doute, gardez celle qui est cochée : la plus petite, elle fonctionne sur la plupart des ordinateurs.
fr.HardwareGpu=Votre ordinateur : %1 Go de mémoire et une carte graphique adaptée à l'IA (%3 Go).
fr.HardwareNoGpu=Votre ordinateur : %1 Go de mémoire, sans carte graphique adaptée à l'IA : les réponses prendront plus de temps.
fr.ModelsNoOllama=Aucune IA n'est encore installée sur cet ordinateur.
fr.ModelsInstalledNone=Aucune IA n'est encore installée sur cet ordinateur.
fr.ModelsInstalledCount=IA déjà installées sur cet ordinateur : %1 (marquées « déjà installée »). En choisir une ne télécharge rien.
fr.ChatNone=Aucune pour l'instant (à choisir plus tard dans les Réglages)
fr.EasyBelowMinimum=Cet ordinateur n'atteint pas la configuration minimale pour faire tourner une IA (8 Go de mémoire et 4 cœurs de processeur) : Bibliobrain ne pourra pas y fonctionner correctement avec une IA, aucune ne sera donc téléchargée.%n%nVous pouvez quand même l'installer et utiliser un fournisseur distant (une API externe) pour poser vos questions, à configurer ensuite dans les Réglages. Vos PDF sont préparés sur cet ordinateur dans tous les cas.%n%nOK pour installer quand même, Annuler pour revenir en arrière.
fr.ModelsNote=Plus une IA est grosse, meilleures sont ses réponses, mais plus elle demande un ordinateur puissant. « Quelques erreurs » et « moins fiable » : nos tests ont relevé des erreurs dans les réponses ou dans les sources citées.
fr.EmbeddingDownload=Télécharger aussi le module de recherche dans vos documents (0,6 Go) : indispensable
fr.EmbeddingInstalled=Le module de recherche dans vos documents est déjà installé
fr.FitGpu=rapide sur cet ordinateur
fr.FitGpuCpu=lente sur cet ordinateur
fr.FitCpu=lente sur cet ordinateur
fr.FitTooBig=trop lourde pour cet ordinateur
fr.FitUnknown=compatibilité avec cet ordinateur inconnue
fr.MarkRecommended=conseillée
fr.MarkInstalled=déjà installée
fr.MarkCaveats=quelques erreurs
fr.MarkLimited=moins fiable
fr.MarkNotTested=non testée par nous

en.ReadyModels=Artificial intelligence:
en.ReadyChat=AI that answers your questions: %1 (to download)
en.ReadyChatInstalled=AI that answers your questions: %1 (already installed)
en.ReadyChatNone=AI that answers your questions: none for now
en.ReadyEmbedding=Search in your documents: module to download
en.ReadyEmbeddingInstalled=Search in your documents: module already installed
en.ReadyEmbeddingNo=Search in your documents: module not downloaded (searching your documents will not work without it)
fr.ReadyModels=Intelligence artificielle :
fr.ReadyChat=IA qui répond à vos questions : %1 (à télécharger)
fr.ReadyChatInstalled=IA qui répond à vos questions : %1 (déjà installée)
fr.ReadyChatNone=IA qui répond à vos questions : aucune pour l'instant
fr.ReadyEmbedding=Recherche dans vos documents : module à télécharger
fr.ReadyEmbeddingInstalled=Recherche dans vos documents : module déjà installé
fr.ReadyEmbeddingNo=Recherche dans vos documents : module non téléchargé (la recherche dans vos documents ne fonctionnera pas sans lui)

en.InstallingComponents=This is the longest step: you can do something else meanwhile, but leave the computer on and its lid open.
en.InstallElapsed=Time elapsed: %1
en.InstallDownload=%1 of %2 downloaded, about %3 left – time elapsed: %4
en.InstallDownloadNoEta=%1 of %2 downloaded – time elapsed: %3
en.InstallBusyBox=An earlier Bibliobrain installation is still running in the background on this computer, so this one stopped without changing anything.%n%nWait for it to finish (it writes its progress to %1), or restart the computer, then run this installer again.
en.InstallFailedBox=The installation could not be completed.%n%nStep that failed: %1%n%nBibliobrain cannot be used yet. Check that you are connected to the Internet and that the disk has enough free space, then run this installer again: it resumes where it stopped.%n%nIf you ask for help, send this file:%n%2
en.FinishedFailHeading=Bibliobrain is not ready yet
en.FinishedFailBody=A step of the installation failed, so Bibliobrain cannot start yet.%n%nRun this installer again: it resumes where it stopped. If it fails again, ask for help and send the file bibliobrain-install.log, in the installation folder.
en.LaunchBibliobrain=Launch Bibliobrain
fr.InstallingComponents=C'est l'étape la plus longue : vous pouvez faire autre chose en attendant, mais laissez l'ordinateur allumé et son écran ouvert.
fr.InstallElapsed=Temps écoulé : %1
fr.InstallDownload=%1 sur %2 téléchargés, encore environ %3 – temps écoulé : %4
fr.InstallDownloadNoEta=%1 sur %2 téléchargés – temps écoulé : %3
fr.InstallBusyBox=Une installation de Bibliobrain lancée plus tôt tourne encore en arrière-plan sur cet ordinateur : celle-ci s'est arrêtée sans rien modifier.%n%nAttendez qu'elle se termine (elle note son avancement dans %1), ou redémarrez l'ordinateur, puis relancez cet installateur.
fr.InstallFailedBox=L'installation n'a pas pu aller jusqu'au bout.%n%nÉtape en échec : %1%n%nBibliobrain n'est pas encore utilisable. Vérifiez que vous êtes connecté à Internet et qu'il reste assez de place sur le disque, puis relancez cet installateur : il reprendra là où il s'est arrêté.%n%nSi vous demandez de l'aide, envoyez ce fichier :%n%2
fr.FinishedFailHeading=Bibliobrain n'est pas encore prêt
fr.FinishedFailBody=Une étape de l'installation a échoué : Bibliobrain ne peut pas encore démarrer.%n%nRelancez cet installateur : il reprendra là où il s'est arrêté. En cas de nouvel échec, demandez de l'aide en envoyant le fichier bibliobrain-install.log, qui se trouve dans le dossier d'installation.
fr.LaunchBibliobrain=Lancer Bibliobrain

; Steps of install.ps1, by the id it gives to Write-Step. %1 is a model name.
en.Step_prepare=Preparing the installation
en.Step_vcredist=Installing a Windows component Bibliobrain needs
en.Step_python=Installing Python, a tool Bibliobrain needs
en.Step_venv=Preparing Bibliobrain
en.Step_torch=Downloading the AI's computing engine (PyTorch)
en.Step_torch_unpack=Unpacking the computing engine (PyTorch, 5 to 30 minutes)
en.Step_torch_check=Checking the computing engine
en.Step_deps=Installing Bibliobrain's components (5 to 15 minutes)
en.Step_deps_check=Checking Bibliobrain's components
en.Step_config=Saving the settings
en.Step_ollama_install=Installing Ollama, the program that runs the AI
en.Step_ollama_update=Updating Ollama, the program that runs the AI
en.Step_ollama_start=Starting Ollama, the program that runs the AI
en.Step_models=Preparing the AI
en.Step_download=Downloading the AI model %1 (this can take a while)
en.Step_reranker=Downloading the module that sorts search results
en.Step_finish=Finishing the installation
en.Step_done=Installation complete
en.Step_busy=Another Bibliobrain installation is already running
fr.Step_prepare=Préparation de l'installation
fr.Step_vcredist=Installation d'un composant Windows nécessaire à Bibliobrain
fr.Step_python=Installation de Python, un outil nécessaire à Bibliobrain
fr.Step_venv=Préparation de Bibliobrain
fr.Step_torch=Téléchargement du moteur de calcul de l'IA (PyTorch)
fr.Step_torch_unpack=Décompression du moteur de calcul (PyTorch, 5 à 30 minutes)
fr.Step_torch_check=Vérification du moteur de calcul
fr.Step_deps=Installation des composants de Bibliobrain (5 à 15 minutes)
fr.Step_deps_check=Vérification des composants de Bibliobrain
fr.Step_config=Enregistrement des réglages
fr.Step_ollama_install=Installation d'Ollama, le programme qui fait tourner l'IA
fr.Step_ollama_update=Mise à jour d'Ollama, le programme qui fait tourner l'IA
fr.Step_ollama_start=Démarrage d'Ollama, le programme qui fait tourner l'IA
fr.Step_models=Préparation de l'IA
fr.Step_download=Téléchargement du modèle d'IA %1 (cela peut être long)
fr.Step_reranker=Téléchargement du module qui trie les résultats de recherche
fr.Step_finish=Finalisation de l'installation
fr.Step_done=Installation terminée
fr.Step_busy=Une autre installation de Bibliobrain est déjà en cours

en.UninstallPageName=Uninstalling Bibliobrain
en.UninstallPageDescription=Bibliobrain is being removed. Your data and settings are kept.
en.UninstallPreparing=Preparing the uninstall…
en.UninstallRemoving=Stopping Bibliobrain and removing its components…
en.UninstallFinishing=Finishing the uninstall…
en.UninstallDone=Uninstall complete.
fr.UninstallPageName=Désinstallation de Bibliobrain
fr.UninstallPageDescription=Bibliobrain est en cours de suppression. Vos données et vos réglages sont conservés.
fr.UninstallPreparing=Préparation de la désinstallation…
fr.UninstallRemoving=Arrêt de Bibliobrain et suppression de ses composants…
fr.UninstallFinishing=Fin de la désinstallation…
fr.UninstallDone=Désinstallation terminée.

[Code]
const
  { Setup's own exit codes stop at 8. This one tells a silent caller (the in-app
    updater, a deployment tool) that the files are in place but install.ps1 failed. }
  InstallScriptFailedExitCode = 20;
  { install.ps1 stops with this code when another run holds the same folder. }
  InstallScriptBusyExitCode = 3;
  WAIT_TIMEOUT = 258;
  CREATE_NO_WINDOW = $08000000;
  STARTF_USESHOWWINDOW = 1;
  STARTUPINFO_SIZE = 68;
  PM_REMOVE = 1;
  ES_CONTINUOUS = $80000000;
  ES_SYSTEM_REQUIRED = $00000001;

type
  { 32-bit layout (Setup is a 32-bit program); pointer members are never used. }
  TStartupInfo = record
    cb: Cardinal;
    lpReserved: Longint;
    lpDesktop: Longint;
    lpTitle: Longint;
    dwX: Cardinal;
    dwY: Cardinal;
    dwXSize: Cardinal;
    dwYSize: Cardinal;
    dwXCountChars: Cardinal;
    dwYCountChars: Cardinal;
    dwFillAttribute: Cardinal;
    dwFlags: Cardinal;
    wShowWindow: Word;
    cbReserved2: Word;
    lpReserved2: Longint;
    hStdInput: Cardinal;
    hStdOutput: Cardinal;
    hStdError: Cardinal;
  end;
  TMessage = record
    hwnd: Longint;
    message: Cardinal;
    wParam: Longint;
    lParam: Longint;
    time: Cardinal;
    ptX: Longint;
    ptY: Longint;
  end;
  TProcessInformation = record
    hProcess: Cardinal;
    hThread: Cardinal;
    dwProcessId: Cardinal;
    dwThreadId: Cardinal;
  end;

var
  ModePage: TInputOptionWizardPage;
  InstallationSummaryPage: TOutputMsgMemoWizardPage;
  ModelsPage: TWizardPage;
  ModelsIntroLabel: TNewStaticText;
  ModelsTop: Integer;
  ModelsPageBuilt: Boolean;
  ModelList: TNewCheckListBox;
  { Indexed like the items of ModelList; 'none' is the "no model for now" choice. }
  ModelNames: array of String;
  ModelInstalled: array of Boolean;
  EmbeddingCheck: TNewCheckBox;
  EmbeddingAlreadyInstalled: Boolean;
  { What the models page found: the probe answered, and the chat models
    already in Ollama ('0' when none). }
  ModelsProbed: Boolean;
  ModelsChatCount: String;
  InstallScriptFailed: Boolean;
  ProgressPath: String;
  LastInstallStep: String;
  { The download install.ps1 reports, in MB, and its seconds left (-1: not known yet). }
  LastDownloadDone: Integer;
  LastDownloadTotal: Integer;
  LastDownloadEta: Integer;

procedure ExitProcess(ExitCode: Integer);
  external 'ExitProcess@kernel32.dll stdcall';

function SetThreadExecutionState(esFlags: Cardinal): Cardinal;
  external 'SetThreadExecutionState@kernel32.dll stdcall';

function CreateProcess(lpApplicationName: String; lpCommandLine: String;
  lpProcessAttributes: Longint; lpThreadAttributes: Longint; bInheritHandles: Boolean;
  dwCreationFlags: Cardinal; lpEnvironment: Longint; lpCurrentDirectory: String;
  var lpStartupInfo: TStartupInfo; var lpProcessInformation: TProcessInformation): Boolean;
  external 'CreateProcessW@kernel32.dll stdcall';

function WaitForSingleObject(hHandle: Cardinal; dwMilliseconds: Cardinal): Cardinal;
  external 'WaitForSingleObject@kernel32.dll stdcall';

function GetExitCodeProcess(hProcess: Cardinal; var lpExitCode: Cardinal): Boolean;
  external 'GetExitCodeProcess@kernel32.dll stdcall';

function CloseHandle(hObject: Cardinal): Boolean;
  external 'CloseHandle@kernel32.dll stdcall';

function GetTickCount: Cardinal;
  external 'GetTickCount@kernel32.dll stdcall';

function PeekMessage(var lpMsg: TMessage; hWnd: Longint; wMsgFilterMin: Cardinal;
  wMsgFilterMax: Cardinal; wRemoveMsg: Cardinal): Boolean;
  external 'PeekMessageW@user32.dll stdcall';

function TranslateMessage(var lpMsg: TMessage): Boolean;
  external 'TranslateMessage@user32.dll stdcall';

function DispatchMessage(var lpMsg: TMessage): Longint;
  external 'DispatchMessageW@user32.dll stdcall';

procedure PumpMessages;
var
  Message: TMessage;
begin
  { Setup's own loop is not running during [Code]: keep the window alive. }
  while PeekMessage(Message, 0, 0, 0, PM_REMOVE) do
  begin
    TranslateMessage(Message);
    DispatchMessage(Message);
  end;
end;

function PowerShellPath: String;
begin
  Result := ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe');
end;

function IsUpdateMode: Boolean;
begin
  { /UPDATEMODE=1 comes from the in-app updater (bibliobrain-manager.ps1 -FromApp),
    which restarts the server itself: no browser tab, and no question either:
    Ollama and its models stay as the first installation and the application's
    settings left them. }
  Result := ExpandConstant('{param:UPDATEMODE|0}') = '1';
end;

function IsEasyMode: Boolean;
begin
  { The simple installation is the first choice and the default: someone who
    only clicks Next gets a working Bibliobrain. The option says what it
    downloads, so choosing it is the consent the custom pages would ask for. }
  Result := (ModePage <> nil) and ModePage.Values[0];
end;

function InstallScriptSucceeded: Boolean;
begin
  Result := not InstallScriptFailed;
end;

function Field(const Line: String; Index: Integer): String;
var
  I, Start, Current: Integer;
begin
  Result := '';
  Current := 0;
  Start := 1;
  for I := 1 to Length(Line) + 1 do
  begin
    if (I > Length(Line)) or (Line[I] = '|') then
    begin
      if Current = Index then
      begin
        Result := Copy(Line, Start, I - Start);
        exit;
      end;
      Current := Current + 1;
      Start := I + 1;
    end;
  end;
end;

function LocalizedNumber(const Value: String): String;
begin
  { install.ps1 writes invariant numbers: 6.1 reads 6,1 in French. }
  Result := Value;
  StringChangeEx(Result, '.', CustomMessage('DecimalSeparator'), True);
end;

function LocalizedSize(const Size: String): String;
begin
  Result := LocalizedNumber(Size);
  StringChangeEx(Result, 'GB', CustomMessage('SizeUnit'), True);
end;

function StepText(const Line: String): String;
var
  Id: String;
begin
  { install.ps1 writes "<id>|<detail>|<English message>". The label shown is
    this file's translation of the id; an unknown id keeps the English message. }
  if Pos('|', Line) = 0 then
  begin
    Result := Line;
    exit;
  end;
  Id := Field(Line, 0);
  Result := Field(Line, 2);
  if Id <> '' then
  begin
    try
      Result := FmtMessage(CustomMessage('Step_' + Id), [Field(Line, 1)]);
    except
    end;
  end;
end;

function FitText(const Code: String): String;
begin
  if Code = 'gpu' then
    Result := CustomMessage('FitGpu')
  else if Code = 'gpucpu' then
    Result := CustomMessage('FitGpuCpu')
  else if Code = 'cpu' then
    Result := CustomMessage('FitCpu')
  else if Code = 'unknown' then
    Result := CustomMessage('FitUnknown')
  else
    Result := CustomMessage('FitTooBig');
end;

function AddModelsText(const Caption: String): TNewStaticText;
begin
  Result := TNewStaticText.Create(ModelsPage);
  Result.Parent := ModelsPage.Surface;
  Result.AutoSize := True;
  Result.WordWrap := True;
  Result.Left := 0;
  Result.Top := ModelsTop;
  Result.Width := ModelsPage.SurfaceWidth;
  Result.Caption := Caption;
  ModelsTop := ModelsTop + Result.Height + ScaleY(6);
end;

procedure RememberModelItem(Index: Integer; const Name: String; Installed: Boolean);
begin
  SetArrayLength(ModelNames, Index + 1);
  SetArrayLength(ModelInstalled, Index + 1);
  ModelNames[Index] := Name;
  ModelInstalled[Index] := Installed;
end;

function AddModelChoice(const Name, Details: String; Installed, Checked: Boolean): Integer;
begin
  { All choices stay at level 0 with no group heading: a heading would start a
    second set of radio buttons, and Inno cannot uncheck a radio button from code. }
  Result := ModelList.AddRadioButton(Name, Details, 0, Checked, True, nil);
  RememberModelItem(Result, Name, Installed);
end;

function ModelDetails(const Line: String): String;
begin
  { model|<name>|<download>|<status>|<fit>|<recommended>|<installed> }
  Result := LocalizedSize(Field(Line, 2)) + ' – ' + FitText(Field(Line, 4));
  if Field(Line, 3) = 'caveats' then
    Result := Result + ', ' + CustomMessage('MarkCaveats')
  else if Field(Line, 3) = 'limited' then
    Result := Result + ', ' + CustomMessage('MarkLimited');
  if Field(Line, 5) = '1' then
    Result := Result + ', ' + CustomMessage('MarkRecommended');
  if Field(Line, 6) = '1' then
    Result := Result + ', ' + CustomMessage('MarkInstalled');
end;

{ When the computer could not be checked: install.ps1's catalog, in its order
  and with its download sizes, but no fit. The smallest tested-enough model is
  selected: it runs on most computers. tests/test_installation.py keeps this
  list in step with $ChatModelCatalog. }
procedure AddUncheckedModelChoices;
begin
  AddModelChoice('qwen3.5:9b', ModelDetails('model|qwen3.5:9b|6.5 GB|validated|unknown|0|0'), False, False);
  AddModelChoice('granite4.2:8b', ModelDetails('model|granite4.2:8b|5.4 GB|caveats|unknown|0|0'), False, False);
  AddModelChoice('qwen3.5:4b', ModelDetails('model|qwen3.5:4b|3.4 GB|caveats|unknown|0|0'), False, False);
  AddModelChoice('gemma4:e2b', ModelDetails('model|gemma4:e2b|7.2 GB|limited|unknown|0|0'), False, False);
  AddModelChoice('qwen3.5:2b', ModelDetails('model|qwen3.5:2b|2.7 GB|limited|unknown|0|0'), False, True);
  AddModelChoice('qwen3.5:0.8b', ModelDetails('model|qwen3.5:0.8b|1.3 GB|limited|unknown|0|0'), False, False);
end;

procedure BuildModelsPage;
var
  ProbePath, Line, Kind, Embedding, OllamaFound, ChatCount: String;
  Lines: TArrayOfString;
  ResultCode, I, NoneIndex, BottomTop: Integer;
  Probed, AnyChecked: Boolean;
  NoteLabel: TNewStaticText;
begin
  ModelsPageBuilt := True;
  ModelsIntroLabel.Caption := CustomMessage('ModelsAnalyzing');
  WizardForm.Refresh;

  { The catalog, the hardware thresholds and the installed models are decided
    by install.ps1 alone, so the wizard never drifts from the console script.
    The folder lets it read the embedding model of an installation being
    updated from its legacy .env. }
  ProbePath := ExpandConstant('{tmp}\bibliobrain-models.txt');
  DeleteFile(ProbePath);
  ExtractTemporaryFile('install-probe.ps1');
  Exec(PowerShellPath,
    '-NoProfile -ExecutionPolicy Bypass -NonInteractive -File "' +
      ExpandConstant('{tmp}\install-probe.ps1') + '" -ProbeModelsFile "' + ProbePath +
      '" -ExistingInstallDir "' + RemoveBackslashUnlessRoot(WizardDirValue) + '"',
    ExpandConstant('{tmp}'), SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Probed := (ResultCode = 0) and LoadStringsFromFile(ProbePath, Lines);
  Log(Format('Model probe: exit code %d, result file %s, usable: %d.', [ResultCode,
    ProbePath, Integer(Probed)]));

  ModelsTop := 0;
  { "Keep the one that is selected" only holds when an AI is recommended. }
  ModelsIntroLabel.Caption := CustomMessage('ModelsIntroNoPick');
  if not Probed then
    ModelsIntroLabel.Caption := '';
  if Probed then
    for I := 0 to GetArrayLength(Lines) - 1 do
      if (Field(Trim(Lines[I]), 0) = 'model') and (Field(Trim(Lines[I]), 5) = '1') then
        ModelsIntroLabel.Caption := CustomMessage('ModelsIntro');
  ModelsTop := ModelsIntroLabel.Top + ModelsIntroLabel.Height + ScaleY(6);

  Embedding := 'embeddinggemma';
  OllamaFound := '';
  ChatCount := '0';
  EmbeddingAlreadyInstalled := False;

  if Probed then
  begin
    for I := 0 to GetArrayLength(Lines) - 1 do
    begin
      Line := Trim(Lines[I]);
      Kind := Field(Line, 0);
      if Kind = 'hardware' then
      begin
        if Field(Line, 3) = '0' then
          AddModelsText(FmtMessage(CustomMessage('HardwareNoGpu'), [Field(Line, 1), Field(Line, 2)]))
        else
          AddModelsText(FmtMessage(CustomMessage('HardwareGpu'), [Field(Line, 1),
            Field(Line, 2), LocalizedNumber(Field(Line, 3))]));
      end
      else if Kind = 'ollama' then
      begin
        OllamaFound := Field(Line, 1);
        ChatCount := Field(Line, 2);
      end
      else if Kind = 'embedding' then
      begin
        Embedding := Field(Line, 1);
        EmbeddingAlreadyInstalled := Field(Line, 2) = '1';
      end;
    end;
    { Say what is already on this computer before offering downloads. }
    if OllamaFound = '0' then
      AddModelsText(CustomMessage('ModelsNoOllama'))
    else if ChatCount = '0' then
      AddModelsText(CustomMessage('ModelsInstalledNone'))
    else
      AddModelsText(FmtMessage(CustomMessage('ModelsInstalledCount'), [ChatCount]));
  end
  else
    AddModelsText(CustomMessage('ModelsProbeFailed'));
  ModelsProbed := Probed;
  ModelsChatCount := ChatCount;

  ModelList := TNewCheckListBox.Create(ModelsPage);
  ModelList.Parent := ModelsPage.Surface;
  ModelList.Left := 0;
  ModelList.Top := ModelsTop;
  ModelList.Width := ModelsPage.SurfaceWidth;
  ModelList.Offset := ScaleX(4);
  SetArrayLength(ModelNames, 0);
  SetArrayLength(ModelInstalled, 0);

  { The catalog in install.ps1's order (tested models, then the small ones),
    then the other chat models already in Ollama. }
  if Probed then
  begin
    for I := 0 to GetArrayLength(Lines) - 1 do
    begin
      Line := Trim(Lines[I]);
      if Field(Line, 0) = 'model' then
        AddModelChoice(Field(Line, 1), ModelDetails(Line), Field(Line, 6) = '1', Field(Line, 5) = '1');
    end;
    for I := 0 to GetArrayLength(Lines) - 1 do
    begin
      Line := Trim(Lines[I]);
      if Field(Line, 0) = 'installed' then
        AddModelChoice(Field(Line, 1), LocalizedSize(Field(Line, 2)) + ' – ' +
          CustomMessage('MarkInstalled') + ', ' + CustomMessage('MarkNotTested'), True, False);
    end;
  end
  else
    AddUncheckedModelChoices;
  NoneIndex := AddModelChoice(CustomMessage('ChatNone'), '', False, False);
  RememberModelItem(NoneIndex, 'none', False);

  AnyChecked := False;
  for I := 0 to ModelList.Items.Count - 1 do
    if ModelList.Checked[I] then
      AnyChecked := True;
  if not AnyChecked then
    ModelList.Checked[NoneIndex] := True;

  { The note and the embedding box sit at the bottom; the list takes the rest
    and scrolls when the computer already has many models. }
  EmbeddingCheck := TNewCheckBox.Create(ModelsPage);
  EmbeddingCheck.Parent := ModelsPage.Surface;
  EmbeddingCheck.Left := 0;
  EmbeddingCheck.Width := ModelsPage.SurfaceWidth;
  EmbeddingCheck.Height := ScaleY(17);
  EmbeddingCheck.Top := ModelsPage.SurfaceHeight - EmbeddingCheck.Height;
  EmbeddingCheck.Checked := True;
  if EmbeddingAlreadyInstalled then
  begin
    EmbeddingCheck.Caption := FmtMessage(CustomMessage('EmbeddingInstalled'), [Embedding]);
    EmbeddingCheck.Enabled := False;
  end
  else
    EmbeddingCheck.Caption := FmtMessage(CustomMessage('EmbeddingDownload'), [Embedding]);

  BottomTop := EmbeddingCheck.Top;
  if Probed then
  begin
    NoteLabel := AddModelsText(CustomMessage('ModelsNote'));
    NoteLabel.Top := EmbeddingCheck.Top - ScaleY(6) - NoteLabel.Height;
    BottomTop := NoteLabel.Top;
  end;
  ModelList.Height := BottomTop - ScaleY(6) - ModelList.Top;
end;

function ChosenChatModel: String;
var
  I: Integer;
begin
  { Silent installs never show the page: their answer comes from /CHATMODEL=
    (a model name, or none), and nothing is downloaded without one. }
  Result := ExpandConstant('{param:CHATMODEL|none}');
  if ModelsPageBuilt then
  begin
    Result := 'none';
    for I := 0 to ModelList.Items.Count - 1 do
      if ModelList.Checked[I] and (ModelNames[I] <> '') then
        Result := ModelNames[I];
  end;
end;

function ChosenChatModelInstalled: Boolean;
var
  I: Integer;
begin
  Result := False;
  if not ModelsPageBuilt then
    exit;
  for I := 0 to ModelList.Items.Count - 1 do
    if ModelList.Checked[I] then
      Result := ModelInstalled[I];
end;

function ChosenEmbeddingDownload: Boolean;
begin
  Result := ExpandConstant('{param:EMBEDDING|0}') = '1';
  if ModelsPageBuilt then
    Result := EmbeddingCheck.Checked and EmbeddingCheck.Enabled;
end;

function InstallScriptArguments: String;
begin
  { Never a prompt: install.ps1 runs without a console, so every answer is
    given here. The application is started from the last page, not by the script. }
  Result := ' -ProgressFile "' + ProgressPath + '" -SkipShortcuts -NoLaunch';
  if IsUpdateMode then
    Result := Result + ' -SkipOllamaSetup'
  else
  begin
    Result := Result + ' -ChatModel "' + ChosenChatModel + '"';
    if ChosenEmbeddingDownload then
      Result := Result + ' -EmbeddingDownload yes'
    else
      Result := Result + ' -EmbeddingDownload no';
  end;
end;

#ifndef PackagingSmoke
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Manager: String;
  ResultCode: Integer;
begin
  Result := '';
  { Running Setup over a started Bibliobrain would replace the code under the
    server and leave pip unable to rewrite the libraries it has loaded. The
    stop is best effort, and there is nothing to stop on a first installation. }
  Manager := ExpandConstant('{app}\scripts\bibliobrain-manager.ps1');
  if FileExists(Manager) then
    Exec(PowerShellPath,
      '-NoProfile -ExecutionPolicy Bypass -File "' + Manager + '" -Action stop',
      ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;
#endif

function UpdateReadyMemo(Space, NewLine, MemoUserInfoInfo, MemoDirInfo, MemoTypeInfo,
  MemoComponentsInfo, MemoGroupInfo, MemoTasksInfo: String): String;
var
  Chat: String;
begin
  Result := MemoDirInfo;
  if MemoTasksInfo <> '' then
    Result := Result + NewLine + NewLine + MemoTasksInfo;
  if (not IsUpdateMode) and ModelsPageBuilt then
  begin
    Chat := ChosenChatModel;
    Result := Result + NewLine + NewLine + CustomMessage('ReadyModels') + NewLine + Space;
    if Chat = 'none' then
      Result := Result + CustomMessage('ReadyChatNone')
    else if ChosenChatModelInstalled then
      Result := Result + FmtMessage(CustomMessage('ReadyChatInstalled'), [Chat])
    else
      Result := Result + FmtMessage(CustomMessage('ReadyChat'), [Chat]);
    Result := Result + NewLine + Space;
    if EmbeddingAlreadyInstalled then
      Result := Result + FmtMessage(CustomMessage('ReadyEmbeddingInstalled'), ['embeddinggemma'])
    else if ChosenEmbeddingDownload then
      Result := Result + FmtMessage(CustomMessage('ReadyEmbedding'), ['embeddinggemma'])
    else
      Result := Result + CustomMessage('ReadyEmbeddingNo');
  end;
end;

function DownloadSizeText(MB: Integer): String;
var
  Tenths: Integer;
begin
  { Decimal units, like the sizes pip and Ollama print in the log. }
  if MB >= 1000 then
  begin
    Tenths := (MB + 50) div 100;
    Result := LocalizedSize(Format('%d.%d GB', [Tenths div 10, Tenths mod 10]));
  end
  else
    Result := IntToStr(MB) + ' ' + CustomMessage('SizeUnitMB');
end;

function EtaText(Seconds: Integer): String;
var
  Minutes: Integer;
begin
  if Seconds < 60 then
  begin
    Result := CustomMessage('EtaUnderMinute');
    exit;
  end;
  Minutes := (Seconds + 59) div 60;
  if Minutes < 60 then
    Result := FmtMessage(CustomMessage('EtaMinutes'), [IntToStr(Minutes)])
  else
    Result := FmtMessage(CustomMessage('EtaHours'), [IntToStr(Minutes div 60),
      IntToStr(Minutes mod 60)]);
end;

procedure ShowInstallProgress(StartTick: Cardinal);
var
  Step: AnsiString;
  Line, Elapsed: String;
  Seconds: Integer;
begin
  { install.ps1 writes "<id>|<detail>|<message>", followed while it downloads
    a large file by "|<MB done>|<MB total>|<seconds left>". }
  if LoadStringFromFile(ProgressPath, Step) then
  begin
    Line := Trim(String(Step));
    if StepText(Line) <> '' then
    begin
      LastInstallStep := StepText(Line);
      LastDownloadDone := StrToIntDef(Field(Line, 3), -1);
      LastDownloadTotal := StrToIntDef(Field(Line, 4), -1);
      LastDownloadEta := StrToIntDef(Field(Line, 5), -1);
    end;
  end;
  Seconds := Integer((GetTickCount - StartTick) div 1000);
  Elapsed := Format('%d:%.2d', [Seconds div 60, Seconds mod 60]);
  WizardForm.StatusLabel.Caption := LastInstallStep;
  if (LastDownloadTotal > 0) and (LastDownloadDone >= 0) and
    (LastDownloadDone <= LastDownloadTotal) then
  begin
    if LastDownloadEta >= 0 then
      WizardForm.FilenameLabel.Caption := FmtMessage(CustomMessage('InstallDownload'), [
        DownloadSizeText(LastDownloadDone), DownloadSizeText(LastDownloadTotal),
        EtaText(LastDownloadEta), Elapsed])
    else
      WizardForm.FilenameLabel.Caption := FmtMessage(CustomMessage('InstallDownloadNoEta'), [
        DownloadSizeText(LastDownloadDone), DownloadSizeText(LastDownloadTotal), Elapsed]);
    if WizardForm.ProgressGauge.Style <> npbstNormal then
    begin
      WizardForm.ProgressGauge.Style := npbstNormal;
      WizardForm.ProgressGauge.Min := 0;
      WizardForm.ProgressGauge.Max := 1000;
    end;
    WizardForm.ProgressGauge.Position := (LastDownloadDone * 1000) div LastDownloadTotal;
  end
  else
  begin
    { Unpacking, installing and checking report no progress: the bar only says
      that the installation is alive. }
    WizardForm.FilenameLabel.Caption := FmtMessage(CustomMessage('InstallElapsed'), [Elapsed]);
    if WizardForm.ProgressGauge.Style <> npbstMarquee then
      WizardForm.ProgressGauge.Style := npbstMarquee;
  end;
end;

function RunHiddenAndWait(const CommandLine, WorkDir: String; var ExitCode: Cardinal): Boolean;
var
  Startup: TStartupInfo;
  Info: TProcessInformation;
  StartTick, Waited: Cardinal;
begin
  Startup.cb := STARTUPINFO_SIZE;
  Startup.dwFlags := STARTF_USESHOWWINDOW;
  Startup.wShowWindow := SW_HIDE;
  { CREATE_NO_WINDOW: a console that has no window, inherited by pip, the
    Python installer and every other tool the script runs. }
  Result := CreateProcess('', CommandLine, 0, 0, False, CREATE_NO_WINDOW, 0, WorkDir,
    Startup, Info);
  if not Result then
  begin
    ExitCode := DLLGetLastError;
    exit;
  end;
  StartTick := GetTickCount;
  repeat
    Waited := WaitForSingleObject(Info.hProcess, 100);
    ShowInstallProgress(StartTick);
    PumpMessages;
  until Waited <> WAIT_TIMEOUT;
  if not GetExitCodeProcess(Info.hProcess, ExitCode) then
    ExitCode := 1;
  CloseHandle(Info.hProcess);
  CloseHandle(Info.hThread);
end;

procedure RunInstallScript;
var
  ExitCode: Cardinal;
  Started: Boolean;
begin
  ProgressPath := ExpandConstant('{tmp}\bibliobrain-install-progress.txt');
  DeleteFile(ProgressPath);
  LastInstallStep := CustomMessage('Step_prepare');
  LastDownloadTotal := -1;
  WizardForm.PageDescriptionLabel.Caption := CustomMessage('InstallingComponents');
  WizardForm.StatusLabel.Caption := LastInstallStep;
  WizardForm.FilenameLabel.Caption := '';
  { The script is long and cannot be resumed from here: no Cancel meanwhile. }
  WizardForm.CancelButton.Enabled := False;
  WizardForm.ProgressGauge.Style := npbstMarquee;
  { A laptop left alone goes to sleep after a few minutes and drops the
    downloads: keep it awake until the script ends (closing the lid still wins). }
  SetThreadExecutionState(ES_CONTINUOUS or ES_SYSTEM_REQUIRED);
  try
    Started := RunHiddenAndWait(
      '"' + PowerShellPath + '" -NoProfile -ExecutionPolicy Bypass -NonInteractive' +
        ' -WindowStyle Hidden -File "' + ExpandConstant('{app}\install.ps1') + '"' +
        InstallScriptArguments,
      ExpandConstant('{app}'), ExitCode);
  finally
    SetThreadExecutionState(ES_CONTINUOUS);
    WizardForm.ProgressGauge.Style := npbstNormal;
    WizardForm.ProgressGauge.Position := WizardForm.ProgressGauge.Max;
    WizardForm.CancelButton.Enabled := True;
  end;
  if Started and (ExitCode = 0) then
    exit;

  InstallScriptFailed := True;
  if Started then
    Log(Format('install.ps1 failed with exit code %d.', [Integer(ExitCode)]))
  else
    Log('install.ps1 could not be started: ' + SysErrorMessage(Integer(ExitCode)));
  if Started and (ExitCode = InstallScriptBusyExitCode) then
  begin
    { Nothing failed: an earlier installation still runs, hidden, and the
      advice of the general message (Internet, disk space) would mislead. }
    SuppressibleMsgBox(
      FmtMessage(CustomMessage('InstallBusyBox'), [ExpandConstant('{app}\bibliobrain-install.log')]),
      mbError, MB_OK, IDOK);
    exit;
  end;
  SuppressibleMsgBox(
    FmtMessage(CustomMessage('InstallFailedBox'), [LastInstallStep,
      ExpandConstant('{app}\bibliobrain-install.log')]),
    mbError, MB_OK, IDOK);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
#ifdef WithInstallScript
  if CurStep = ssPostInstall then
    RunInstallScript;
#endif
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  { Setup still steps through the pages of a silent installation, but nobody
    answers: its choices come from /CHATMODEL= and /EMBEDDING=. }
  if (CurPageID = ModelsPage.ID) and not ModelsPageBuilt and not WizardSilent then
    BuildModelsPage;
  if (CurPageID = wpFinished) and InstallScriptFailed then
  begin
    WizardForm.FinishedHeadingLabel.Caption := CustomMessage('FinishedFailHeading');
    WizardForm.FinishedLabel.Caption := CustomMessage('FinishedFailBody');
  end;
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := False;
  if ModePage = nil then
    exit;
  { Nothing to choose when the in-app updater reuses the installed models. }
  if IsUpdateMode then
    Result := (PageID = ModelsPage.ID) or (PageID = ModePage.ID)
  else if IsEasyMode then
    { When the computer could not be checked, the simple installation still
      asks which AI to use instead of choosing none. }
    Result := (PageID = wpSelectDir) or (PageID = InstallationSummaryPage.ID) or
      (PageID = wpSelectTasks) or ((PageID = ModelsPage.ID) and ModelsProbed);
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  Description: String;
begin
  Result := True;
  { The simple installation never shows the models page, but still needs its
    answer: the AI recommended for this computer and the search module, both
    checked by default. }
  if (CurPageID = ModePage.ID) and IsEasyMode and not ModelsPageBuilt and not WizardSilent then
  begin
    Description := WizardForm.PageDescriptionLabel.Caption;
    WizardForm.PageDescriptionLabel.Caption := CustomMessage('ModelsAnalyzing');
    WizardForm.NextButton.Enabled := False;
    WizardForm.Refresh;
    try
      BuildModelsPage;
    finally
      WizardForm.NextButton.Enabled := True;
      WizardForm.PageDescriptionLabel.Caption := Description;
    end;
  end;
  { Below the minimum configuration nothing fits and nothing is installed: say
    so before installing without an AI. Cancel goes back to the choice, where
    the custom installation still lets them pick one. }
  if (CurPageID = ModePage.ID) and IsEasyMode and ModelsPageBuilt and ModelsProbed
    and not WizardSilent and (ChosenChatModel = 'none') and (ModelsChatCount = '0') then
  begin
    if MsgBox(CustomMessage('EasyBelowMinimum'), mbInformation, MB_OKCANCEL) <> IDOK then
      Result := False;
  end;
end;

procedure DeinitializeSetup;
begin
  { Setup has no API for its own exit code and would otherwise report success. }
  if InstallScriptFailed then
    ExitProcess(InstallScriptFailedExitCode);
end;

procedure InitializeWizard;
begin
  ModePage := CreateInputOptionPage(wpWelcome, CustomMessage('ModeTitle'),
    CustomMessage('ModeSubtitle'), CustomMessage('ModeHint'), True, False);
  ModePage.Add(CustomMessage('ModeEasy'));
  ModePage.Add(CustomMessage('ModeCustom'));
  ModePage.Values[0] := True;
  { Inno leaves the margin before a check box at 4 pixels whatever the display
    scale: on an enlarged display the box is cut on its left. }
  ModePage.CheckListBox.Offset := ScaleX(4);
  WizardForm.TasksList.Offset := ScaleX(4);
  InstallationSummaryPage := CreateOutputMsgMemoPage(
    wpSelectDir,
    CustomMessage('SummaryTitle'),
    CustomMessage('SummarySubtitle'),
    CustomMessage('SummaryHint'),
    CustomMessage('SummaryBody')
  );
  ModelsPage := CreateCustomPage(wpSelectTasks, CustomMessage('ModelsTitle'),
    CustomMessage('ModelsSubtitle'));
  ModelsTop := 0;
  ModelsIntroLabel := AddModelsText(CustomMessage('ModelsIntro'));
end;

procedure InitializeUninstallProgressForm;
begin
  UninstallProgressForm.PageNameLabel.Caption := CustomMessage('UninstallPageName');
  UninstallProgressForm.PageDescriptionLabel.Caption := CustomMessage('UninstallPageDescription');
  UninstallProgressForm.StatusLabel.Caption := CustomMessage('UninstallPreparing');
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  { The progress form is not created during a silent uninstall, and not yet
    at usAppMutexCheck, the first step: touching it there is a fatal runtime
    error ("Could not call proc") that aborts the uninstall. }
  if UninstallSilent or (UninstallProgressForm = nil) then
    exit;

  case CurUninstallStep of
    usUninstall:
      UninstallProgressForm.StatusLabel.Caption := CustomMessage('UninstallRemoving');
    usPostUninstall:
      UninstallProgressForm.StatusLabel.Caption := CustomMessage('UninstallFinishing');
    usDone:
      UninstallProgressForm.StatusLabel.Caption := CustomMessage('UninstallDone');
  end;
end;
