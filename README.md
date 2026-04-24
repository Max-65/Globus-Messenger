# Globus Messenger

Globus Messenger is a multi-service web messenger prototype with role-based access control and real-time messaging.  
The project combines a browser UI, a Python FastAPI backend, and a Go WebSocket chat service.

## Features

- JWT-based authentication
- Real-time chat via WebSocket
- Private and general chats
- Role-based access for `admin`, `teacher`, and `student`
- Teacher tools for uploading materials and assigning tasks
- Admin tools for creating and deleting users
- Persistent storage with SQLite
- File delivery through S3-compatible object storage

## Tech Stack

- Frontend: HTML, JavaScript
- Backend: Python, FastAPI
- Realtime service: Go, WebSocket
- Database: SQLite
- Auth: JWT
- Storage: S3-compatible API
- Deployment: Docker

## Project Structure

- `frontend/` — client-side interface
- `python_core/` — main API, authentication, materials, tasks, admin logic
- `go_chat/` — WebSocket message service