package main

import (
	"database/sql"
	"log"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/golang-jwt/jwt/v4"
	"github.com/gorilla/websocket"
	_ "github.com/mattn/go-sqlite3"
)

var upgrader = websocket.Upgrader{
	CheckOrigin: func(r *http.Request) bool { return true },
}

// Глобальная карта клиентов: ChatID -> Map[UserID]Connection
var hubs = make(map[string]map[int]*websocket.Conn)
var mu sync.Mutex

var db *sql.DB

const SECRET_KEY = "supersecretkey" // Тот же, что в Python модуле

type Message struct {
	Type       string `json:"type"` // "msg", "reply", "join"
	ChatID     string `json:"chat_id"`
	SenderID   int    `json:"sender_id"`
	Username   string `json:"username,omitempty"`
	Text       string `json:"text"`
	ReplyToID  *int   `json:"reply_to_id,omitempty"`
	SenderName string `json:"sender_name"`
}

func main() {
	var err error
	db, err = sql.Open("sqlite3", "../database/globus.db")
	if err != nil {
		log.Fatal(err)
	}
	defer db.Close()

	// Инициализация таблицы сообщений (в Python модуле она не создается)
	query := `CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY, chat_id TEXT, sender_id INTEGER,
  text TEXT, reply_to_id INTEGER, timestamp TIMESTAMP)`
	db.Exec(query)

	http.HandleFunc("/ws", handleConnections)
	log.Println("Go Chat Service started on :8083")
	log.Fatal(http.ListenAndServe(":8083", nil))
}

func handleConnections(w http.ResponseWriter, r *http.Request) {
	// 1. Получаем токен из URL
	tokenString := r.URL.Query().Get("token")
	claims := jwt.MapClaims{}
	_, err := jwt.ParseWithClaims(tokenString, claims, func(token *jwt.Token) (interface{}, error) {
		return []byte(SECRET_KEY), nil
	})

	if err != nil {
		http.Error(w, "Unauthorized", http.StatusUnauthorized)
		return
	}

	// 2. Апгрейд до WebSocket
	ws, err := upgrader.Upgrade(w, r, nil)
	if err != nil {
		log.Fatal(err)
	}
	defer ws.Close()

	userID := int(claims["id"].(float64))
	username := claims["sub"].(string)

	// Простая подписка: клиент присылает первое сообщение {"type": "join", "chat_id": "math_class"}
	// В продакшене это делается автоматически на основе прав
	for {
		var msg Message
		err := ws.ReadJSON(&msg)
		if err != nil {
			// Удаляем пользователя из хабов при разрыве
			mu.Lock()
			if _, ok := hubs[msg.ChatID]; ok {
				delete(hubs[msg.ChatID], userID)
			}
			mu.Unlock()
			break
		}

		msg.SenderID = userID
		msg.Username = username

		if msg.Type == "join" {
			// Проверка: если чат приватный, имеет ли пользователь право доступа?
			if strings.HasPrefix(msg.ChatID, "private_") {
				// Формат ChatID: private_user1ID_user2ID
				parts := strings.Split(msg.ChatID, "_")
				if len(parts) != 3 {
					continue // Некорректный ID
				}

				id1, _ := strconv.Atoi(parts[1])
				id2, _ := strconv.Atoi(parts[2])

				if userID != id1 && userID != id2 {
					log.Printf("Warning: User %d tried to join private chat %s", userID, msg.ChatID)
					continue
				}
			}
			mu.Lock()
			if hubs[msg.ChatID] == nil {
				hubs[msg.ChatID] = make(map[int]*websocket.Conn)
			}
			hubs[msg.ChatID][userID] = ws
			mu.Unlock()

			loadHistory(ws, msg.ChatID)
			continue
		}

		if msg.Type == "msg" {
			// Сохраняем в БД
			res, _ := db.Exec("INSERT INTO messages (chat_id, sender_id, text, reply_to_id, timestamp) VALUES (?, ?, ?, ?, ?)",
				msg.ChatID, msg.SenderID, msg.Text, msg.ReplyToID, time.Now())
			rows, _ := db.Query("SELECT full_name FROM users WHERE id=?", msg.SenderID)
			defer rows.Close()
			for rows.Next() {
				rows.Scan(&msg.SenderName)
			}
			// Получаем ID сообщения для фронтенда
			id, _ := res.LastInsertId()
			msgID := int(id)

			// Отправляем всем в комнате
			broadcast(msg, msgID)
		}
	}
}

func loadHistory(ws *websocket.Conn, chatID string) {
	query := `
		SELECT m.text, u.full_name, m.sender_id, m.reply_to_id
		FROM messages AS m
		JOIN users AS u ON m.sender_id = u.id
		WHERE m.chat_id = ?
		ORDER BY m.id ASC LIMIT 50`
	rows, err := db.Query(query, chatID)
	if err != nil {
		return
	}
	defer rows.Close()

	for rows.Next() {
		var m Message
		rows.Scan(&m.Text, &m.SenderName, &m.SenderID, &m.ReplyToID)
		m.Type = "history"
		ws.WriteJSON(m)
	}
}

func broadcast(msg Message, msgID int) {
	mu.Lock()
	defer mu.Unlock()

	// Формируем JSON ответа
	response := map[string]interface{}{
		"id":          msgID,
		"sender_name": msg.SenderName,
		"text":        msg.Text,
		"reply_to":    msg.ReplyToID,
		"type":        "msg",
	}

	for _, client := range hubs[msg.ChatID] {
		err := client.WriteJSON(response)
		if err != nil {
			client.Close()
			delete(hubs[msg.ChatID], msg.SenderID)
		}
	}
}
