# Jenkins Build Projects

Three simple projects for verifying Jenkins build pipelines on a local Windows Jenkins server.

---

## Projects Overview

| Project | Language | Artifact | Build Tool |
|---|---|---|---|
| `maven-jar-project` | Java | `.jar` | Maven 3 |
| `csharp-dll-project` | C# | `.dll` | dotnet CLI |
| `dotnet-console-app` | C# | `.exe` | dotnet CLI |

---

## Jenkins Server Requirements

### 1. Maven JAR Project
Install on Jenkins server:
- **JDK 11** (or later)
- **Apache Maven 3.x**

Configure in Jenkins → **Manage Jenkins → Global Tool Configuration**:
- Add JDK → name it `JDK 11`
- Add Maven → name it `Maven 3`

### 2. C# DLL Project
Install on Jenkins server:
- **.NET 6.0 SDK** → https://dotnet.microsoft.com/download/dotnet/6.0
- Verify: `dotnet --version`

No extra Jenkins plugin needed — uses `dotnet` CLI directly.

### 3. .NET Console App
Same requirement as above:
- **.NET 6.0 SDK**

---

## Setting Up Jenkins Pipelines

For each project, create a **Pipeline job** in Jenkins:

1. **New Item** → Enter name → Select **Pipeline** → OK
2. Under **Pipeline** section:
   - Definition: `Pipeline script from SCM`
   - SCM: `Git`
   - Repository URL: your repo URL
   - Script Path: `projects/<project-folder>/Jenkinsfile`
3. Save → **Build Now**

---

## Project Structure

```
projects/
├── maven-jar-project/
│   ├── src/main/java/com/example/App.java
│   ├── pom.xml
│   └── Jenkinsfile
│
├── csharp-dll-project/
│   ├── MathLibrary/
│   │   ├── MathHelper.cs
│   │   └── MathLibrary.csproj
│   ├── MathLibrary.sln
│   └── Jenkinsfile
│
└── dotnet-console-app/
    ├── HelloApp/
    │   ├── Program.cs
    │   └── HelloApp.csproj
    ├── HelloApp.sln
    └── Jenkinsfile
```
