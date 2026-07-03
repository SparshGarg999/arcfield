// Package config provides environment-based configuration for the service.
package config

import (
	"fmt"
	"os"
	"strconv"
	"time"
)

// Config holds all configuration values for the service.
type Config struct {
	// Server
	ServerPort         int
	ServerReadTimeout  time.Duration
	ServerWriteTimeout time.Duration
	ShutdownTimeout    time.Duration

	// Database
	DBHost     string
	DBPort     int
	DBUser     string
	DBPassword string
	DBName     string
	DBSSLMode  string

	// Idempotency
	IdempotencyKeyTTL time.Duration
	CleanupInterval   time.Duration
}

// Load reads configuration from environment variables with sensible defaults.
func Load() (*Config, error) {
	cfg := &Config{
		ServerPort:         envInt("SERVER_PORT", 8080),
		ServerReadTimeout:  envDuration("SERVER_READ_TIMEOUT", 10*time.Second),
		ServerWriteTimeout: envDuration("SERVER_WRITE_TIMEOUT", 10*time.Second),
		ShutdownTimeout:    envDuration("SHUTDOWN_TIMEOUT", 15*time.Second),

		DBHost:     envStr("DB_HOST", "localhost"),
		DBPort:     envInt("DB_PORT", 5432),
		DBUser:     envStr("DB_USER", "arcfield"),
		DBPassword: envStr("DB_PASSWORD", "arcfield"),
		DBName:     envStr("DB_NAME", "arcfield"),
		DBSSLMode:  envStr("DB_SSLMODE", "disable"),

		IdempotencyKeyTTL: envDuration("IDEMPOTENCY_KEY_TTL", 24*time.Hour),
		CleanupInterval:   envDuration("CLEANUP_INTERVAL", 1*time.Hour),
	}

	return cfg, nil
}

// DSN returns the PostgreSQL connection string.
func (c *Config) DSN() string {
	return fmt.Sprintf(
		"host=%s port=%d user=%s password=%s dbname=%s sslmode=%s",
		c.DBHost, c.DBPort, c.DBUser, c.DBPassword, c.DBName, c.DBSSLMode,
	)
}

func envStr(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

func envInt(key string, fallback int) int {
	if v := os.Getenv(key); v != "" {
		n, err := strconv.Atoi(v)
		if err != nil {
			return fallback
		}
		return n
	}
	return fallback
}

func envDuration(key string, fallback time.Duration) time.Duration {
	if v := os.Getenv(key); v != "" {
		d, err := time.ParseDuration(v)
		if err != nil {
			return fallback
		}
		return d
	}
	return fallback
}
