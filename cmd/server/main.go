// Package main is the entry point for the arcfield game economy service.
package main

import (
	"context"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/SparshGarg999/arcfield/internal/config"
	"github.com/SparshGarg999/arcfield/internal/database"
)

func main() {
	log.SetFlags(log.Ldate | log.Ltime | log.Lmicroseconds | log.Lshortfile)
	log.Println("starting arcfield service...")

	// Load configuration
	cfg, err := config.Load()
	if err != nil {
		log.Fatalf("failed to load config: %v", err)
	}

	// Connect to database with retry (Postgres may not be ready yet in Docker)
	log.Println("connecting to database...")
	db, err := database.ConnectWithRetry(cfg.DSN(), 10, 1*time.Second)
	if err != nil {
		log.Fatalf("failed to connect to database: %v", err)
	}
	defer db.Close()
	log.Println("database connected")

	// Run migrations
	log.Println("running migrations...")
	if err := database.Migrate(db); err != nil {
		log.Fatalf("failed to run migrations: %v", err)
	}
	log.Println("migrations complete")

	// Build HTTP mux
	mux := http.NewServeMux()

	// Health check endpoint
	mux.HandleFunc("GET /health", func(w http.ResponseWriter, r *http.Request) {
		if err := db.PingContext(r.Context()); err != nil {
			w.WriteHeader(http.StatusServiceUnavailable)
			fmt.Fprintf(w, `{"status":"unhealthy","error":"%s"}`, err.Error())
			return
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		fmt.Fprint(w, `{"status":"healthy"}`)
	})

	// Placeholder: API routes will be added in Phase 3
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusNotFound)
		fmt.Fprint(w, `{"error":"not found"}`)
	})

	// Configure HTTP server
	srv := &http.Server{
		Addr:         fmt.Sprintf(":%d", cfg.ServerPort),
		Handler:      mux,
		ReadTimeout:  cfg.ServerReadTimeout,
		WriteTimeout: cfg.ServerWriteTimeout,
	}

	// Graceful shutdown: listen for SIGINT/SIGTERM
	shutdown := make(chan os.Signal, 1)
	signal.Notify(shutdown, syscall.SIGINT, syscall.SIGTERM)

	// Start server in a goroutine
	serverErr := make(chan error, 1)
	go func() {
		log.Printf("listening on %s", srv.Addr)
		serverErr <- srv.ListenAndServe()
	}()

	// Wait for shutdown signal or server error
	select {
	case sig := <-shutdown:
		log.Printf("received signal %v, shutting down...", sig)
	case err := <-serverErr:
		if err != nil && err != http.ErrServerClosed {
			log.Printf("server error: %v", err)
		}
	}

	// Graceful shutdown with timeout
	ctx, cancel := context.WithTimeout(context.Background(), cfg.ShutdownTimeout)
	defer cancel()

	if err := srv.Shutdown(ctx); err != nil {
		log.Printf("graceful shutdown failed: %v, forcing close", err)
		srv.Close()
	}

	log.Println("arcfield service stopped")
}
