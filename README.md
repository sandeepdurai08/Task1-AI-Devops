# 🤖 Jenkins AI Chatbot for Google Chat

An AI-powered chatbot that integrates with **Google Chat** to let your team interact with **Jenkins** using natural language. Powered by **Ollama** for local AI processing.

## ✨ Features

- 💬 **Google Chat Integration** - Works directly in your team's chat
- 🧠 **Natural Language Understanding** - Ask questions in plain English
- 📋 **Rich Cards** - Beautiful interactive cards with buttons
- 🔒 **Secure** - Google token verification for production use
- 📊 **Job Status** - Check any job's status instantly
- 📜 **Build Logs** - Fetch console output
- 🚀 **Trigger Builds** - Start builds with confirmation

## 🚀 Quick Start

```powershell
# 1. Setup
cd "d:\ai module\Task1-AI-Devops"
python -m venv venv
.\venv\Scripts\activate
pip install -r requirements.txt

# 2. Configure
copy .env.example .env
# Edit .env with your Jenkins/Ollama/Google settings

# 3. Run
python google_chat_bot.py
```

**📖 For complete step-by-step instructions, see [SETUP_GUIDE.md](SETUP_GUIDE.md)**

## 💬 Usage Examples

Once the bot is added to Google Chat, you can ask:

| Message | What it does |
|---------|--------------|
| `list jobs` | Shows all Jenkins jobs in a card |
| `status of deploy-app` | Detailed job status with buttons |
| `logs for my-pipeline` | Fetches build console output |
| `run test-job` | Triggers build (with confirmation) |
| `help` | Shows available commands |

## 📁 Project Structure

```
Task1-AI-Devops/
├── google_chat_bot.py   # 🌐 Google Chat webhook server (main!)
├── chatbot.py           # 💻 CLI version (terminal mode)
├── jenkins_client.py    # 🔧 Jenkins API integration
├── ollama_client.py     # 🧠 Ollama AI integration
├── chat_cards.py        # 🎨 Google Chat card builder
├── requirements.txt     # 📦 Python dependencies
├── .env.example         # ⚙️ Configuration template
├── SETUP_GUIDE.md       # 📖 Complete A-Z setup guide
└── README.md            # This file
```

## 🏗️ Architecture

```
┌──────────────┐      ┌──────────────┐      ┌──────────────┐
│ Google Chat  │─────▶│ Flask Server │─────▶│   Ollama     │
│   (User)     │ POST │ (Webhook)    │      │   (AI)       │
└──────────────┘      └──────┬───────┘      └──────────────┘
                             │
                             ▼
                      ┌──────────────┐
                      │   Jenkins    │
                      │   (CI/CD)    │
                      └──────────────┘
```

## 🔧 Two Modes Available

### Mode 1: Google Chat Bot (Recommended)
```powershell
python google_chat_bot.py
```
Runs a Flask server that receives webhooks from Google Chat.

### Mode 2: Terminal CLI
```powershell
python chatbot.py
```
Interactive terminal chatbot for local testing.

## 📖 Documentation

- **[SETUP_GUIDE.md](SETUP_GUIDE.md)** - Complete A-Z setup instructions
- Includes: Ollama setup, Jenkins token, Google Cloud config, deployment

## 🛠️ Requirements

- Python 3.10+
- Jenkins server with API access
- Ollama running locally
- Google Workspace account (for Chat app)
- Google Cloud project (free tier works)

## 🔒 Security

- Google token verification enabled by default
- Supports HTTPS for production
- API tokens stored in environment variables
- No hardcoded credentials

## 📝 License

MIT License - feel free to use and modify!
