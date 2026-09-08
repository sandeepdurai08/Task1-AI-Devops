# 🚀 Complete A-to-Z Setup Guide: Jenkins Chatbot for Google Chat

This guide walks you through setting up the Jenkins AI Chatbot from scratch.

---

## 📋 Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                        ARCHITECTURE                             │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│   Google Chat ──▶ Your Server ──▶ Ollama ──▶ Jenkins           │
│     (User)        (Flask App)     (AI)       (CI/CD)           │
│                                                                 │
│   1. User types message in Google Chat                         │
│   2. Google sends it to your server (webhook)                  │
│   3. Ollama understands the request                            │
│   4. Server fetches data from Jenkins                          │
│   5. Response sent back to Google Chat                         │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

---

## 📝 Prerequisites Checklist

Before starting, make sure you have:

- [ ] Python 3.10 or higher installed
- [ ] Access to a Jenkins server (with admin rights to create API token)
- [ ] A Google Workspace account (Gmail won't work for Chat apps)
- [ ] A Google Cloud account (free tier works)
- [ ] Ollama installed on your server

---

## STEP 1: Install Ollama (AI Engine)

Ollama runs the AI model locally on your machine.

### Windows:
1. Download from: https://ollama.ai/download
2. Run the installer
3. Open PowerShell and run:
```powershell
# Pull the AI model (this downloads ~4GB)
ollama pull llama3

# Verify it's working
ollama list
```

### Linux:
```bash
curl -fsSL https://ollama.ai/install.sh | sh
ollama pull llama3
```

### Verify Ollama is Running:
```powershell
# Should return a JSON response
curl http://localhost:11434/api/tags
```

---

## STEP 2: Get Jenkins API Token

You need an API token to let the bot access Jenkins.

### Steps:
1. **Log into Jenkins** with your account
2. **Click your username** (top-right corner)
3. Click **Configure** (left sidebar)
4. Scroll to **API Token** section
5. Click **Add new Token**
6. Give it a name like "chatbot-token"
7. Click **Generate**
8. **⚠️ COPY THE TOKEN NOW** - you won't see it again!

### Test Your Token:
```powershell
# Replace with your values
$JENKINS_URL = "http://your-jenkins:8080"
$JENKINS_USER = "your-username"
$JENKINS_TOKEN = "your-token"

# Test the connection (should return JSON)
curl -u "${JENKINS_USER}:${JENKINS_TOKEN}" "${JENKINS_URL}/api/json"
```

---

## STEP 3: Set Up Google Cloud Project

### 3.1 Create a Google Cloud Project

1. Go to [Google Cloud Console](https://console.cloud.google.com)
2. Click the project dropdown (top-left) → **New Project**
3. Enter a name: `jenkins-chatbot`
4. Click **Create**
5. Wait for project to be created, then select it

### 3.2 Enable the Google Chat API

1. In Cloud Console, go to **APIs & Services** → **Library**
2. Search for "Google Chat API"
3. Click on it → Click **Enable**

### 3.3 Get Your Project Number

1. Go to **Cloud Console Dashboard**
2. Look for **Project number** (NOT Project ID!)
3. It's a number like: `123456789012`
4. **Save this** - you'll need it for auth verification

---

## STEP 4: Configure the Google Chat App

### 4.1 Open Chat API Configuration

1. In Cloud Console, go to **APIs & Services** → **Enabled APIs**
2. Click on **Google Chat API**
3. Click **Configuration** tab

### 4.2 Fill in App Details

| Field | Value |
|-------|-------|
| **App name** | Jenkins Bot |
| **Avatar URL** | (optional) URL to an image |
| **Description** | AI-powered Jenkins assistant |
| **Interactive features** | ✅ Enable |
| **Connection settings** | App URL |
| **App URL** | `https://your-server-url.com/` |
| **Visibility** | Choose who can use the bot |

### 4.3 About the App URL

Your server needs a **public HTTPS URL**. Options:

#### Option A: ngrok (For Testing)
```powershell
# Install ngrok: https://ngrok.com/download
# Start your Flask app first, then:
ngrok http 5000

# You'll get a URL like: https://abc123.ngrok.io
# Use this as your App URL
```

#### Option B: Cloud Deployment (For Production)
- **Google Cloud Run** - Easiest, auto-scales
- **AWS EC2/Lambda** - If you use AWS
- **Azure App Service** - If you use Azure
- **Heroku** - Simple deployment

### 4.4 Save Configuration

1. Click **Save**
2. The app is now created!

---

## STEP 5: Set Up Your Server

### 5.1 Clone/Navigate to Project

```powershell
cd "d:\ai module\Task1-AI-Devops"
```

### 5.2 Create Virtual Environment

```powershell
# Create virtual environment
python -m venv venv

# Activate it
.\venv\Scripts\activate

# You should see (venv) in your prompt
```

### 5.3 Install Dependencies

```powershell
pip install -r requirements.txt
```

### 5.4 Configure Environment Variables

```powershell
# Copy the example file
copy .env.example .env

# Open and edit .env with your values
notepad .env
```

Edit `.env` with your actual values:

```env
# Jenkins
JENKINS_URL=http://your-jenkins:8080
JENKINS_USER=your-username
JENKINS_API_TOKEN=your-actual-token

# Ollama
OLLAMA_URL=http://localhost:11434
OLLAMA_MODEL=llama3

# Google Chat
GOOGLE_CHAT_PROJECT_NUMBER=123456789012
VERIFY_GOOGLE_TOKEN=true

# Server
PORT=5000
```

---

## STEP 6: Run the Bot

### For Development/Testing:

```powershell
# Make sure venv is activated
.\venv\Scripts\activate

# Run the bot
python google_chat_bot.py
```

You should see:
```
INFO:__main__:Jenkins client initialized for http://your-jenkins:8080
INFO:__main__:Ollama client initialized (llama3)
INFO:__main__:Starting Google Chat bot on port 5000
 * Running on http://0.0.0.0:5000
```

### For Production:

```powershell
# Use gunicorn for production
gunicorn -w 4 -b 0.0.0.0:5000 google_chat_bot:app
```

---

## STEP 7: Expose Your Server (For Testing with ngrok)

In a **new terminal**:

```powershell
# Start ngrok tunnel
ngrok http 5000
```

Copy the HTTPS URL (like `https://abc123.ngrok.io`) and:
1. Go back to Google Cloud Console
2. Update your Chat App's **App URL** with this new URL
3. Save

---

## STEP 8: Add the Bot to Google Chat

### 8.1 Find Your Bot

1. Open [Google Chat](https://chat.google.com)
2. Click **+ Start a chat** (or the + button)
3. Click **Find apps**
4. Search for "Jenkins Bot" (or your app name)
5. Click on it → **Add**

### 8.2 Start Chatting!

Send a message to your bot:
```
What jobs do you have?
```

The bot should respond with a list of Jenkins jobs!

---

## STEP 9: Test the Bot

Try these commands:

| Message | Expected Response |
|---------|-------------------|
| `list jobs` | Shows all Jenkins jobs in a card |
| `status of my-job` | Shows detailed status |
| `help` | Shows available commands |
| `run my-job` | Asks for confirmation to trigger build |

---

## 🔧 Troubleshooting

### "Bot not responding"

1. Check if your server is running:
   ```powershell
   curl http://localhost:5000/health
   ```

2. Check if ngrok is connected (if using ngrok)

3. Check server logs for errors

### "Cannot connect to Jenkins"

1. Verify Jenkins URL is correct
2. Test API token manually:
   ```powershell
   curl -u "user:token" "http://jenkins:8080/api/json"
   ```

### "Token verification failed"

1. Make sure `GOOGLE_CHAT_PROJECT_NUMBER` is correct (it's the number, not the ID!)
2. For testing, you can set `VERIFY_GOOGLE_TOKEN=false` in `.env`

### "Ollama not responding"

1. Check if Ollama is running:
   ```powershell
   curl http://localhost:11434/api/tags
   ```

2. Make sure the model is pulled:
   ```powershell
   ollama list
   ```

---

## 📁 Project Files Summary

```
Task1-AI-Devops/
├── google_chat_bot.py    # Main webhook server (RUN THIS!)
├── jenkins_client.py     # Jenkins API integration
├── ollama_client.py      # Ollama AI integration  
├── chat_cards.py         # Google Chat card builder
├── chatbot.py            # CLI version (optional)
├── requirements.txt      # Python dependencies
├── .env.example          # Configuration template
├── .env                  # Your configuration (create this)
├── README.md             # Quick reference
└── SETUP_GUIDE.md        # This file
```

---

## 🚀 Production Deployment Tips

### Deploy to Google Cloud Run (Recommended)

1. Create a `Dockerfile`:
```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
CMD ["gunicorn", "-b", "0.0.0.0:8080", "google_chat_bot:app"]
```

2. Deploy:
```bash
gcloud run deploy jenkins-chatbot \
  --source . \
  --region us-central1 \
  --allow-unauthenticated
```

3. Update your Chat App URL with the Cloud Run URL

### Security Best Practices

- ✅ Always use HTTPS
- ✅ Keep `VERIFY_GOOGLE_TOKEN=true` in production
- ✅ Store secrets in environment variables, not code
- ✅ Use a separate Jenkins user with limited permissions
- ✅ Regularly rotate API tokens

---

## 🎉 Done!

Your Jenkins AI Chatbot is now running on Google Chat!

Users can now ask about Jenkins jobs in natural language, and the bot will respond with beautiful cards showing job statuses, build logs, and action buttons.

**Need help?** Check the logs with:
```powershell
# In your server terminal, you'll see all incoming requests and any errors
```
