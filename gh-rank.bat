@echo off
chcp 65001 >nul
title gh-rank
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0launcher.ps1"