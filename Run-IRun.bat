@echo off
rem SPDX-License-Identifier: MPL-2.0
setlocal
pushd "%~dp0" || (
    echo Could not open the IRun source folder.
    pause
    exit /b 1
)

set "IRUN_PYTHON=.venv-irun\Scripts\python.exe"
if not exist "%IRUN_PYTHON%" (
    echo Creating the IRun Python environment...
    py -3.12 -c "import sys" >nul 2>&1
    if errorlevel 1 (
        python -c "import sys; sys.exit(sys.version_info[:2] != (3, 12))" >nul 2>&1
        if errorlevel 1 (
            where uv >nul 2>&1
            if errorlevel 1 (
                echo Install 64-bit Python 3.12 from https://www.python.org/downloads/windows/ and run this file again.
                goto :failed
            )
            uv venv --no-project --python 3.12 --seed .venv-irun || goto :failed
        ) else (
            python -m venv .venv-irun || goto :failed
        )
    ) else (
        py -3.12 -m venv .venv-irun || goto :failed
    )
)

"%IRUN_PYTHON%" -c "import sys; sys.exit(sys.version_info[:2] != (3, 12) or sys.maxsize <= 2**32)" >nul 2>&1
if errorlevel 1 (
    echo The .venv-irun folder is not using 64-bit Python 3.12. Rename it and run this file again.
    goto :failed
)

echo Checking IRun dependencies...
where uv >nul 2>&1
if errorlevel 1 (
    "%IRUN_PYTHON%" -m pip --disable-pip-version-check install --quiet -r requirements.txt || goto :failed
) else (
    uv pip install --python "%IRUN_PYTHON%" --quiet -r requirements.txt || goto :failed
)
"%IRUN_PYTHON%" main.py %*
if errorlevel 1 goto :failed

popd
exit /b 0

:failed
echo.
echo IRun stopped with an error. Review the message above.
popd
pause
exit /b 1
