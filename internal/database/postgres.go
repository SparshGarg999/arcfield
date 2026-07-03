// Package database provides PostgreSQL connection management and migration support.
package database

import (
	"context"
	"database/sql"
	"embed"
	"fmt"
	"log"
	"sort"
	"strings"
	"time"

	_ "github.com/lib/pq"
)

//go:embed migrations/*.sql
var migrationsFS embed.FS

// Connect opens a PostgreSQL connection pool and verifies connectivity.
func Connect(dsn string) (*sql.DB, error) {
	db, err := sql.Open("postgres", dsn)
	if err != nil {
		return nil, fmt.Errorf("database open: %w", err)
	}

	// Connection pool settings for a small service
	db.SetMaxOpenConns(25)
	db.SetMaxIdleConns(5)
	db.SetConnMaxLifetime(5 * time.Minute)

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	if err := db.PingContext(ctx); err != nil {
		db.Close()
		return nil, fmt.Errorf("database ping: %w", err)
	}

	return db, nil
}

// ConnectWithRetry attempts to connect to the database with exponential backoff.
// This is essential in Docker where the app container may start before Postgres is ready.
func ConnectWithRetry(dsn string, maxAttempts int, initialDelay time.Duration) (*sql.DB, error) {
	var db *sql.DB
	var err error
	delay := initialDelay

	for attempt := 1; attempt <= maxAttempts; attempt++ {
		db, err = Connect(dsn)
		if err == nil {
			return db, nil
		}

		if attempt < maxAttempts {
			log.Printf("database connection attempt %d/%d failed: %v (retrying in %v)", attempt, maxAttempts, err, delay)
			time.Sleep(delay)
			delay *= 2 // exponential backoff
			if delay > 30*time.Second {
				delay = 30 * time.Second
			}
		}
	}

	return nil, fmt.Errorf("database connection failed after %d attempts: %w", maxAttempts, err)
}

// Migrate runs all SQL migration files in order.
// Migrations are embedded in the binary at compile time.
func Migrate(db *sql.DB) error {
	entries, err := migrationsFS.ReadDir("migrations")
	if err != nil {
		return fmt.Errorf("read migrations dir: %w", err)
	}

	// Sort migration files by name (lexicographic ordering = execution order)
	sort.Slice(entries, func(i, j int) bool {
		return entries[i].Name() < entries[j].Name()
	})

	// Create migrations tracking table if it doesn't exist
	_, err = db.Exec(`
		CREATE TABLE IF NOT EXISTS schema_migrations (
			filename TEXT PRIMARY KEY,
			applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
		)
	`)
	if err != nil {
		return fmt.Errorf("create migrations table: %w", err)
	}

	for _, entry := range entries {
		if entry.IsDir() || !strings.HasSuffix(entry.Name(), ".sql") {
			continue
		}

		// Check if already applied
		var count int
		err := db.QueryRow("SELECT COUNT(*) FROM schema_migrations WHERE filename = $1", entry.Name()).Scan(&count)
		if err != nil {
			return fmt.Errorf("check migration %s: %w", entry.Name(), err)
		}
		if count > 0 {
			log.Printf("migration %s already applied, skipping", entry.Name())
			continue
		}

		// Read and execute migration
		content, err := migrationsFS.ReadFile("migrations/" + entry.Name())
		if err != nil {
			return fmt.Errorf("read migration %s: %w", entry.Name(), err)
		}

		log.Printf("applying migration: %s", entry.Name())
		if _, err := db.Exec(string(content)); err != nil {
			return fmt.Errorf("execute migration %s: %w", entry.Name(), err)
		}

		// Record migration
		if _, err := db.Exec("INSERT INTO schema_migrations (filename) VALUES ($1)", entry.Name()); err != nil {
			return fmt.Errorf("record migration %s: %w", entry.Name(), err)
		}

		log.Printf("migration %s applied successfully", entry.Name())
	}

	return nil
}
