# Jenkins Build Dependencies — Manual Installation Guide

Install each tool in order. Complete one step fully before moving to the next.

---

## Prerequisites Check

Open PowerShell and run these first to see what's already installed:

```powershell
java -version
mvn -version
dotnet --version
git --version
```

Your machine already has:
- ✅ Java 21
- ✅ Git 2.45

You need to install:
- ❌ Maven 3.9.9
- ❌ .NET 6.0 SDK
- ❌ Jenkins LTS

---

## Step 1 — Install Apache Maven 3.9.16

> ✅ You already have the ZIP downloaded. Default location checked:
> `%USERPROFILE%\Downloads\apache-maven-3.9.16-bin.zip`
> Run `setup-maven.bat` as Administrator to do steps 1.3–1.5 automatically,
> OR follow manually below.

### 1.1 Download
Already downloaded. Confirm it is at:
```
%USERPROFILE%\Downloads\apache-maven-3.9.16-bin.zip
```

### 1.2 Verify Checksum (Security Step)
```powershell
(Get-FileHash -Path "$env:USERPROFILE\Downloads\apache-maven-3.9.16-bin.zip" -Algorithm SHA256).Hash
```
https://maven.apache.org/download.cgi

> If they don't match — delete the file and re-download. Do not proceed.

### 1.3 Extract
Open PowerShell **as Administrator** and run:
```powershell
Expand-Archive -Path "$env:USERPROFILE\Downloads\apache-maven-3.9.16-bin.zip" -DestinationPath "C:\Program Files\Maven" -Force
```

### 1.4 Set Environment Variables
Open **Start Menu** → search **"Edit the system environment variables"** → click **Environment Variables**

Under **System Variables**:

1. Click **New**
   - Variable name: `MAVEN_HOME`
   - Variable value: `C:\Program Files\Maven\apache-maven-3.9.16`
   - Click OK

2. Find **Path** in the list → click **Edit** → click **New** → add:
   ```
   C:\Program Files\Maven\apache-maven-3.9.16\bin
   ```
   Click OK → OK → OK

### 1.5 Verify
Open a **new** PowerShell window (important — old windows won't see the new PATH):
```powershell
mvn -version
```
Expected output:
```
Apache Maven 3.9.16
Java version: 21.x.x
```

---

## Step 2 — Install .NET 6.0 SDK

### 2.1 Download
Go to: https://dotnet.microsoft.com/download/dotnet/6.0

Click **SDK 6.0.x** → **Windows** → **x64 Installer** → download the `.exe` file.

Or download directly:
```powershell
Invoke-WebRequest -Uri "https://dot.net/v1/dotnet-install.ps1" -OutFile "$env:USERPROFILE\Downloads\dotnet-install.ps1"
```

### 2.2 Install

**Option A — Using the .exe installer (recommended):**
- Double-click the downloaded `.exe`
- Follow the installer wizard
- It adds `dotnet` to PATH automatically

**Option B — Using the PowerShell script:**
```powershell
powershell -ExecutionPolicy Bypass -File "$env:USERPROFILE\Downloads\dotnet-install.ps1" -Channel 6.0 -InstallDir "C:\Program Files\dotnet"
```

Then add to PATH manually (same as Step 1.4):
- Add new System Variable: `DOTNET_ROOT` = `C:\Program Files\dotnet`
- Add to **Path**: `C:\Program Files\dotnet`

### 2.3 Verify
Open a **new** PowerShell window:
```powershell
dotnet --version
```
Expected output:
```
6.0.xxx
```

---

## Step 3 — Install Jenkins LTS

### 3.1 Download
Go to: https://www.jenkins.io/download/

Under **LTS Release** → click **Windows** → download `jenkins.msi`

Or direct link:
```powershell
Invoke-WebRequest -Uri "https://get.jenkins.io/windows-stable/latest/jenkins.msi" -OutFile "$env:USERPROFILE\Downloads\jenkins.msi"
```

### 3.2 Verify Download Size
```powershell
(Get-Item "$env:USERPROFILE\Downloads\jenkins.msi").Length / 1MB
```
> Should be **greater than 80 MB**. If it's a few KB, the download failed — try again.

### 3.3 Run the Installer
- Double-click `jenkins.msi`
- Click **Next**
- **Destination Folder**: leave as default (`C:\Program Files\Jenkins`)
- **Service Logon**: choose **Run service as LocalSystem** (simplest for local dev)
- **Port**: leave as `8080`
- **Java Home**: the installer will auto-detect your Java 21 path
- Click **Install** → allow UAC prompt → click **Finish**

### 3.4 Verify Jenkins Service is Running
```powershell
Get-Service -Name Jenkins
```
Expected output:
```
Status   Name     DisplayName
------   ----     -----------
Running  Jenkins  Jenkins
```

If status is **Stopped**, start it:
```powershell
Start-Service -Name Jenkins
```

### 3.5 Get the Initial Admin Password
```powershell
Get-Content "C:\ProgramData\Jenkins\.jenkins\secrets\initialAdminPassword"
```
> Copy this password — you'll need it in Step 4.

### 3.6 Open Jenkins
Open your browser and go to: **http://localhost:8080**

---

## Step 4 — Configure Jenkins (First-Time Setup)

### 4.1 Unlock Jenkins
- Paste the password from Step 3.5
- Click **Continue**

### 4.2 Install Plugins
- Choose **Install suggested plugins**
- Wait for all plugins to finish installing (~2–5 minutes)

### 4.3 Create Admin User
- Fill in username, password, full name, email
- Click **Save and Continue** → **Save and Finish** → **Start using Jenkins**

---

## Step 5 — Configure Build Tools in Jenkins

### 5.1 Go to Global Tool Configuration
**Manage Jenkins** → **Global Tool Configuration**

### 5.2 Add JDK
- Scroll to **JDK** section → click **Add JDK**
- Uncheck **Install automatically**
- Name: `JDK 21`  ← must match exactly what the Jenkinsfile expects
- JAVA_HOME: paste your Java path

  To find your Java path:
  ```powershell
  (Get-Command java).Source
  # e.g. C:\Program Files\Java\jdk-21\bin\java.exe
  # JAVA_HOME = C:\Program Files\Java\jdk-21
  ```

### 5.3 Add Maven
- Scroll to **Maven** section → click **Add Maven**
- Uncheck **Install automatically**
- Name: `Maven 3`  ← must match exactly
- MAVEN_HOME: `C:\Program Files\Maven\apache-maven-3.9.16`

### 5.4 Save
Click **Save** at the bottom of the page.

---

## Step 6 — Restart Jenkins (Pick Up New PATH)

After installing Maven and .NET, Jenkins service must be restarted so it sees the updated system PATH.

```powershell
# Run PowerShell as Administrator
Restart-Service -Name Jenkins
```

Or via Jenkins UI:
- Go to **http://localhost:8080/restart**
- Click **Yes**

---

## Step 7 — Verify All Tools Inside Jenkins

Create a quick test pipeline to confirm Jenkins can reach all tools:

1. **New Item** → name it `verify-tools` → select **Pipeline** → OK
2. Scroll to **Pipeline** section, paste this script:

```groovy
pipeline {
    agent any
    tools {
        maven 'Maven 3'
        jdk   'JDK 11'
    }
    stages {
        stage('Check Tools') {
            steps {
                bat 'java -version'
                bat 'mvn -version'
                bat 'dotnet --version'
                bat 'git --version'
            }
        }
    }
}
```

3. Click **Save** → **Build Now**
4. Click the build number → **Console Output**

All 4 tools should print their versions. If any fail, check the PATH and tool config in Step 5.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `mvn` not found after install | Open a new terminal — old terminals don't see PATH changes |
| `dotnet` not found | Reboot or run `Restart-Service Jenkins` after .NET install |
| Jenkins stuck at unlock screen | Check password with `Get-Content C:\ProgramData\Jenkins\.jenkins\secrets\initialAdminPassword` |
| Jenkins service won't start | Check Java: `java -version` must work; Jenkins needs Java on PATH |
| Build fails with "tool not found" | Tool name in Jenkinsfile must exactly match the name in Global Tool Configuration |
| Port 8080 already in use | Change Jenkins port: edit `C:\Program Files\Jenkins\jenkins.xml`, find `--httpPort=8080` and change it |

---

## Summary — What You Installed

| Tool | Version | Location |
|---|---|---|
| Java | 21 (existing) | Already on PATH |
| Git | 2.45 (existing) | Already on PATH |
| Maven | 3.9.16 | `C:\Program Files\Maven\apache-maven-3.9.16` |
| .NET SDK | 6.0.x | `C:\Program Files\dotnet` |
| Jenkins | LTS latest | `http://localhost:8080` |
