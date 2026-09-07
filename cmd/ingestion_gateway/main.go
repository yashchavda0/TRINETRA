package main

import (
	"context"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"log"
	"math"
	"net"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/confluentinc/confluent-kafka-go/v2/kafka"
	"github.com/gin-gonic/gin"
	"google.golang.org/protobuf/encoding/protowire"
)

const (
	kafkaTopic       = "surveillance-events-raw"
	maxProtobufBytes = 2 * 1024 * 1024
)

type SurveillanceEvent struct {
	EventID        string
	CameraID       string
	DepartmentCode int32
	TimestampUTCMs int64
}

type TCPConnectionPool struct {
	mu          sync.Mutex
	pools       map[string]chan net.Conn
	maxPerHost  int
	dialTimeout time.Duration
}

func NewTCPConnectionPool(maxPerHost int, dialTimeout time.Duration) *TCPConnectionPool {
	return &TCPConnectionPool{
		pools:       make(map[string]chan net.Conn),
		maxPerHost:  maxPerHost,
		dialTimeout: dialTimeout,
	}
}

func (p *TCPConnectionPool) getPool(addr string) chan net.Conn {
	p.mu.Lock()
	defer p.mu.Unlock()

	if ch, ok := p.pools[addr]; ok {
		return ch
	}

	ch := make(chan net.Conn, p.maxPerHost)
	p.pools[addr] = ch
	return ch
}

func (p *TCPConnectionPool) Acquire(ctx context.Context, addr string) (net.Conn, error) {
	bucket := p.getPool(addr)
	select {
	case conn := <-bucket:
		if conn != nil {
			return conn, nil
		}
	default:
	}

	dialer := net.Dialer{Timeout: p.dialTimeout, KeepAlive: 30 * time.Second}
	conn, err := dialer.DialContext(ctx, "tcp", addr)
	if err != nil {
		return nil, fmt.Errorf("dial %s: %w", addr, err)
	}
	return conn, nil
}

func (p *TCPConnectionPool) Release(addr string, conn net.Conn) {
	if conn == nil {
		return
	}

	bucket := p.getPool(addr)
	select {
	case bucket <- conn:
	default:
		_ = conn.Close()
	}
}

func (p *TCPConnectionPool) Close() {
	p.mu.Lock()
	defer p.mu.Unlock()

	for addr, bucket := range p.pools {
		close(bucket)
		for conn := range bucket {
			_ = conn.Close()
		}
		delete(p.pools, addr)
	}
}

type IngestionGateway struct {
	producer *kafka.Producer
	topic    string

	deliveryWG sync.WaitGroup
}

func NewIngestionGateway(producer *kafka.Producer, topic string) *IngestionGateway {
	g := &IngestionGateway{
		producer: producer,
		topic:    topic,
	}

	g.deliveryWG.Add(1)
	go g.deliveryLoop()
	return g
}

func (g *IngestionGateway) deliveryLoop() {
	defer g.deliveryWG.Done()
	for ev := range g.producer.Events() {
		msg, ok := ev.(*kafka.Message)
		if !ok {
			continue
		}
		if msg.TopicPartition.Error != nil {
			log.Printf("kafka delivery failure topic=%s partition=%d offset=%v err=%v",
				safeTopicName(msg.TopicPartition.Topic),
				msg.TopicPartition.Partition,
				msg.TopicPartition.Offset,
				msg.TopicPartition.Error,
			)
		}
	}
}

func (g *IngestionGateway) Close(flushTimeout time.Duration) {
	remaining := g.producer.Flush(int(flushTimeout.Milliseconds()))
	if remaining > 0 {
		log.Printf("kafka flush timeout, %d message(s) still pending", remaining)
	}
	g.producer.Close()
	g.deliveryWG.Wait()
}

func (g *IngestionGateway) IngestHandler(c *gin.Context) {
	if err := verifyMutualTLS(c.Request); err != nil {
		c.JSON(http.StatusUnauthorized, gin.H{"error": err.Error()})
		return
	}

	contentType := strings.ToLower(c.GetHeader("Content-Type"))
	if !strings.HasPrefix(contentType, "application/x-protobuf") {
		c.JSON(http.StatusUnsupportedMediaType, gin.H{"error": "Content-Type must be application/x-protobuf"})
		return
	}

	body, err := io.ReadAll(io.LimitReader(c.Request.Body, maxProtobufBytes+1))
	if err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": fmt.Sprintf("unable to read request body: %v", err)})
		return
	}
	if len(body) > maxProtobufBytes {
		c.JSON(http.StatusRequestEntityTooLarge, gin.H{"error": "protobuf payload exceeds 2MB limit"})
		return
	}

	event, err := parseSurveillanceEvent(body)
	if err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": fmt.Sprintf("invalid SurveillanceEvent protobuf: %v", err)})
		return
	}

	if strings.TrimSpace(event.CameraID) == "" {
		c.JSON(http.StatusBadRequest, gin.H{"error": "camera_id is required"})
		return
	}
	if event.TimestampUTCMs <= 0 {
		c.JSON(http.StatusBadRequest, gin.H{"error": "timestamp_utc_ms must be present and > 0"})
		return
	}

	eventID := strings.TrimSpace(event.EventID)
	if eventID == "" {
		eventID = newUUIDv4()
	}

	key := strconv.FormatInt(int64(event.DepartmentCode), 10)
	err = g.producer.Produce(&kafka.Message{
		TopicPartition: kafka.TopicPartition{Topic: &g.topic, Partition: kafka.PartitionAny},
		Key:            []byte(key),
		Value:          body,
		Headers: []kafka.Header{
			{Key: "event_id", Value: []byte(eventID)},
			{Key: "camera_id", Value: []byte(event.CameraID)},
		},
	}, nil)
	if err != nil {
		c.JSON(http.StatusServiceUnavailable, gin.H{"error": fmt.Sprintf("kafka produce enqueue failed: %v", err)})
		return
	}

	c.JSON(http.StatusAccepted, gin.H{
		"status":   "QUEUED",
		"event_id": eventID,
	})
}

func parseSurveillanceEvent(payload []byte) (*SurveillanceEvent, error) {
	event := &SurveillanceEvent{}
	remaining := payload

	for len(remaining) > 0 {
		fieldNum, wireType, n := protowire.ConsumeTag(remaining)
		if n < 0 {
			return nil, protowire.ParseError(n)
		}
		remaining = remaining[n:]

		switch fieldNum {
		case 1:
			if wireType != protowire.BytesType {
				return nil, fmt.Errorf("event_id has invalid wire type: %v", wireType)
			}
			v, consumed := protowire.ConsumeBytes(remaining)
			if consumed < 0 {
				return nil, protowire.ParseError(consumed)
			}
			event.EventID = string(v)
			remaining = remaining[consumed:]
		case 2:
			if wireType != protowire.BytesType {
				return nil, fmt.Errorf("camera_id has invalid wire type: %v", wireType)
			}
			v, consumed := protowire.ConsumeBytes(remaining)
			if consumed < 0 {
				return nil, protowire.ParseError(consumed)
			}
			event.CameraID = string(v)
			remaining = remaining[consumed:]
		case 3:
			if wireType != protowire.VarintType {
				return nil, fmt.Errorf("department_code has invalid wire type: %v", wireType)
			}
			v, consumed := protowire.ConsumeVarint(remaining)
			if consumed < 0 {
				return nil, protowire.ParseError(consumed)
			}
			event.DepartmentCode = int32(v)
			remaining = remaining[consumed:]
		case 4:
			if wireType != protowire.VarintType {
				return nil, fmt.Errorf("timestamp_utc_ms has invalid wire type: %v", wireType)
			}
			v, consumed := protowire.ConsumeVarint(remaining)
			if consumed < 0 {
				return nil, protowire.ParseError(consumed)
			}
			if v > math.MaxInt64 {
				return nil, fmt.Errorf("timestamp_utc_ms overflows int64")
			}
			event.TimestampUTCMs = int64(v)
			remaining = remaining[consumed:]
		default:
			consumed := protowire.ConsumeFieldValue(fieldNum, wireType, remaining)
			if consumed < 0 {
				return nil, protowire.ParseError(consumed)
			}
			remaining = remaining[consumed:]
		}
	}

	return event, nil
}

func preflightKafkaBrokers(ctx context.Context, pool *TCPConnectionPool, bootstrapServers string) error {
	if strings.TrimSpace(bootstrapServers) == "" {
		return errors.New("bootstrap servers cannot be empty")
	}

	for _, raw := range strings.Split(bootstrapServers, ",") {
		broker := strings.TrimSpace(raw)
		if broker == "" {
			continue
		}

		if !strings.Contains(broker, ":") {
			broker += ":9092"
		}

		conn, err := pool.Acquire(ctx, broker)
		if err != nil {
			return fmt.Errorf("broker preflight failed for %s: %w", broker, err)
		}
		pool.Release(broker, conn)
	}

	return nil
}

func verifyMutualTLS(r *http.Request) error {
	if r.TLS == nil {
		return errors.New("mTLS is required")
	}
	if len(r.TLS.PeerCertificates) == 0 {
		return errors.New("client certificate is missing")
	}
	if len(r.TLS.VerifiedChains) == 0 {
		return errors.New("client certificate chain was not verified")
	}

	leaf := r.TLS.PeerCertificates[0]
	now := time.Now()
	if now.Before(leaf.NotBefore) || now.After(leaf.NotAfter) {
		return errors.New("client certificate is expired or not yet valid")
	}

	if len(leaf.ExtKeyUsage) > 0 {
		hasClientAuth := false
		for _, usage := range leaf.ExtKeyUsage {
			if usage == x509.ExtKeyUsageClientAuth || usage == x509.ExtKeyUsageAny {
				hasClientAuth = true
				break
			}
		}
		if !hasClientAuth {
			return errors.New("client certificate does not permit client authentication")
		}
	}

	return nil
}

func mustBuildServerTLSConfig(certFile, keyFile, caFile string) (*tls.Config, error) {
	cert, err := tls.LoadX509KeyPair(certFile, keyFile)
	if err != nil {
		return nil, fmt.Errorf("load server certificate/key: %w", err)
	}

	caPEM, err := os.ReadFile(caFile)
	if err != nil {
		return nil, fmt.Errorf("read CA file: %w", err)
	}
	caPool := x509.NewCertPool()
	if ok := caPool.AppendCertsFromPEM(caPEM); !ok {
		return nil, errors.New("unable to parse CA certificate")
	}

	return &tls.Config{
		MinVersion:   tls.VersionTLS12,
		Certificates: []tls.Certificate{cert},
		ClientAuth:   tls.RequireAndVerifyClientCert,
		ClientCAs:    caPool,
	}, nil
}

func safeTopicName(topic *string) string {
	if topic == nil {
		return "<nil>"
	}
	return *topic
}

func envOrDefault(key, fallback string) string {
	if v := strings.TrimSpace(os.Getenv(key)); v != "" {
		return v
	}
	return fallback
}

func requiredEnv(key string) (string, error) {
	v := strings.TrimSpace(os.Getenv(key))
	if v == "" {
		return "", fmt.Errorf("%s is required", key)
	}
	return v, nil
}

func newUUIDv4() string {
	b := make([]byte, 16)
	if _, err := rand.Read(b); err != nil {
		panic(fmt.Sprintf("crypto/rand failed: %v", err))
	}

	b[6] = (b[6] & 0x0f) | 0x40
	b[8] = (b[8] & 0x3f) | 0x80

	hexStr := hex.EncodeToString(b)
	return fmt.Sprintf("%s-%s-%s-%s-%s",
		hexStr[0:8],
		hexStr[8:12],
		hexStr[12:16],
		hexStr[16:20],
		hexStr[20:32],
	)
}

func main() {
	listenAddr := envOrDefault("INGEST_LISTEN_ADDR", ":8443")
	bootstrapServers := envOrDefault("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")

	certFile, err := requiredEnv("INGEST_TLS_CERT_FILE")
	if err != nil {
		log.Fatalf("configuration error: %v", err)
	}
	keyFile, err := requiredEnv("INGEST_TLS_KEY_FILE")
	if err != nil {
		log.Fatalf("configuration error: %v", err)
	}
	caFile, err := requiredEnv("INGEST_TLS_CA_FILE")
	if err != nil {
		log.Fatalf("configuration error: %v", err)
	}

	tlsConfig, err := mustBuildServerTLSConfig(certFile, keyFile, caFile)
	if err != nil {
		log.Fatalf("failed to build TLS config: %v", err)
	}

	probePool := NewTCPConnectionPool(2, 3*time.Second)
	defer probePool.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	if err := preflightKafkaBrokers(ctx, probePool, bootstrapServers); err != nil {
		cancel()
		log.Fatalf("kafka preflight check failed: %v", err)
	}
	cancel()

	producer, err := kafka.NewProducer(&kafka.ConfigMap{
		"bootstrap.servers":          bootstrapServers,
		"acks":                       "1",
		"compression.type":           "snappy",
		"linger.ms":                  10,
		"batch.num.messages":         10000,
		"go.delivery.reports":        true,
		"socket.keepalive.enable":    true,
		"queue.buffering.max.kbytes": 512000,
	})
	if err != nil {
		log.Fatalf("unable to create Kafka producer: %v", err)
	}

	gateway := NewIngestionGateway(producer, kafkaTopic)
	defer gateway.Close(5 * time.Second)

	gin.SetMode(gin.ReleaseMode)
	router := gin.New()
	router.Use(gin.Logger(), gin.Recovery())
	router.POST("/api/v1/events/ingest", gateway.IngestHandler)

	srv := &http.Server{
		Addr:              listenAddr,
		Handler:           router,
		TLSConfig:         tlsConfig,
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       10 * time.Second,
		WriteTimeout:      10 * time.Second,
		IdleTimeout:       120 * time.Second,
		MaxHeaderBytes:    1 << 20,
	}

	listener, err := tls.Listen("tcp", listenAddr, tlsConfig)
	if err != nil {
		log.Fatalf("failed to bind listener: %v", err)
	}

	serverErrCh := make(chan error, 1)
	go func() {
		if serveErr := srv.Serve(listener); serveErr != nil && !errors.Is(serveErr, http.ErrServerClosed) {
			serverErrCh <- serveErr
		}
	}()

	log.Printf("ingestion gateway listening on %s", listenAddr)

	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)

	select {
	case sig := <-sigCh:
		log.Printf("shutdown signal received: %s", sig.String())
	case serveErr := <-serverErrCh:
		log.Printf("server terminated with error: %v", serveErr)
	}

	shutdownCtx, shutdownCancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer shutdownCancel()

	if err := srv.Shutdown(shutdownCtx); err != nil {
		log.Printf("http shutdown error: %v", err)
	}
}
