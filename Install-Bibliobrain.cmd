@echo off
setlocal
cd /d "%~dp0"
set "INSTALL_LOG=%~dp0bibliobrain-install.log"
title Installation de Bibliobrain

echo.
echo  Bibliobrain va installer :
echo   - l'application et un environnement Python isole ;
echo   - Python 3.14 et le runtime Microsoft Visual C++ s'ils sont absents ;
echo   - PyTorch et les bibliotheques necessaires ;
echo   - Ollama, puis les modeles que vous choisirez (EmbeddingGemma, modele de conversation) ;
echo   - le modele de reranking rapide.
echo.
echo  OvisOCR2, GLM-OCR et le grand modele de recherche approfondie ne seront
echo  installes qu'apres une confirmation separee dans Bibliobrain.
echo  Les composants deja presents seront conserves.
echo  Plusieurs gigaoctets peuvent etre telecharges. Une connexion Internet est requise.
echo  L'installation des dependances Python peut durer 5 a 15 minutes.
echo  Ne fermez pas cette fenetre meme si une etape reste silencieuse plusieurs minutes.
echo.

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
if errorlevel 1 (
    echo.
    echo L'installation a rencontre une erreur. Le message ci-dessus indique l'action a effectuer.
    echo Log complet : "%INSTALL_LOG%"
    pause
    exit /b 1
)

echo.
echo Installation terminee. Bibliobrain est pret a etre utilise.
echo Log complet : "%INSTALL_LOG%"
pause
