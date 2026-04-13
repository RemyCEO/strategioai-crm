@echo off
chcp 65001 >nul
cd /d %~dp0

echo =============================================
echo  StrategioAI CRM - Foerste gangs oppsett
echo =============================================
echo.
echo Aapner Supabase SQL Editor i nettleser...
echo SQL er kopiert til utklippstavlen.
echo.
echo Gjor dette:
echo  1. Logg inn pa Supabase (strategioai@strategioai.com)
echo  2. Lim inn SQL (Ctrl+V) i editoren
echo  3. Klikk "Run" (grønn knapp)
echo  4. Lukk denne vinduet og kjoer start.bat
echo.

python -c "
import subprocess, sys
sql = open('schema.sql', encoding='utf-8').read()

# Kopier til utklippstavlen
p = subprocess.Popen(['clip'], stdin=subprocess.PIPE)
p.communicate(input=sql.encode('utf-16'))

# Aapne Supabase SQL Editor
import webbrowser
webbrowser.open('https://supabase.com/dashboard/project/kizkootcsoigvkdejpwx/sql/new')
print('SQL kopiert til utklippstavlen og nettleser aapnet!')
"

pause
