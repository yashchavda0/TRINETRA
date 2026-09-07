package main

import (
	"bufio"
	"context"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"os/signal"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/gin-gonic/gin"
)

type PlayRequest struct {
	CameraID string `json:"camera_id"`
	RTSPURL  string `json:"rtsp_url"`
	SDPOffer string `json:"sdp_offer"`
}

type RelaySession struct {
	ID        string
	CameraID  string
	RTSPURL   string
	SDPOffer  string
	SDPAnswer string

	cmd    *exec.Cmd
	cancel context.CancelFunc

	lastHeartbeat atomic.Int64
}

type RTSPConnectionPool struct {
	mu          sync.Mutex
	buckets     map[string]chan net.Conn
	maxPerHost  int
	dialTimeout time.Duration
}

func NewRTSPConnectionPool(maxPerHost int, dialTimeout time.Duration) *RTSPConnectionPool {
	return &RTSPConnectionPool{
		buckets:     make(map[string]chan net.Conn),
		maxPerHost:  maxPerHost,
		dialTimeout: dialTimeout,
	}
}

func (p *RTSPConnectionPool) getBucket(addr string) chan net.Conn {
	p.mu.Lock()
	defer p.mu.Unlock()

	if bucket, ok := p.buckets[addr]; ok {
		return bucket
	}

	bucket := make(chan net.Conn, p.maxPerHost)
	p.buckets[addr] = bucket
	return bucket
}

func (p *RTSPConnectionPool) Acquire(ctx context.Context, addr string) (net.Conn, error) {
	bucket := p.getBucket(addr)
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
		return nil, fmt.Errorf("dial %s failed: %w", addr, err)
	}
	return conn, nil
}

func (p *RTSPConnectionPool) Release(addr string, conn net.Conn) {
	if conn == nil {
		return
	}

	if err := conn.SetDeadline(time.Time{}); err != nil {
		_ = conn.Close()
		return
	}

	bucket := p.getBucket(addr)
	select {
	case bucket <- conn:
	default:
		_ = conn.Close()
	}
}

func (p *RTSPConnectionPool) Close() {
	p.mu.Lock()
	defer p.mu.Unlock()

	for addr, bucket := range p.buckets {
		close(bucket)
		for conn := range bucket {
			_ = conn.Close()
		}
		delete(p.buckets, addr)
	}
}

type SessionManager struct {
	mu         sync.RWMutex
	sessions   map[string]*RelaySession
	inactivity time.Duration
	connPool   *RTSPConnectionPool
}

func NewSessionManager(inactivity time.Duration, connPool *RTSPConnectionPool) *SessionManager {
	return &SessionManager{
		sessions:   make(map[string]*RelaySession),
		inactivity: inactivity,
		connPool:   connPool,
	}
}

func (sm *SessionManager) StartSession(ctx context.Context, req PlayRequest) (*RelaySession, error) {
	hostPort, err := rtspHostPort(req.RTSPURL)
	if err != nil {
		return nil, err
	}

	conn, err := sm.connPool.Acquire(ctx, hostPort)
	if err != nil {
		return nil, fmt.Errorf("rtsp probe connection failed: %w", err)
	}

	if err := probeRTSP(conn, req.RTSPURL); err != nil {
		_ = conn.Close()
		return nil, fmt.Errorf("rtsp probe failed: %w", err)
	}
	sm.connPool.Release(hostPort, conn)

	videoPort, err := freeUDPPort()
	if err != nil {
		return nil, fmt.Errorf("allocate RTP port: %w", err)
	}

	sessionID := newUUIDv4()
	sdpAnswer, err := buildSDPAnswer(req.SDPOffer, videoPort)
	if err != nil {
		return nil, err
	}

	procCtx, procCancel := context.WithCancel(context.Background())
	cmd := buildFFmpegCommand(procCtx, req.RTSPURL, videoPort)
	stderr, err := cmd.StderrPipe()
	if err != nil {
		procCancel()
		return nil, fmt.Errorf("open ffmpeg stderr pipe: %w", err)
	}

	if err := cmd.Start(); err != nil {
		procCancel()
		return nil, fmt.Errorf("start ffmpeg process failed: %w", err)
	}

	session := &RelaySession{
		ID:        sessionID,
		CameraID:  req.CameraID,
		RTSPURL:   req.RTSPURL,
		SDPOffer:  req.SDPOffer,
		SDPAnswer: sdpAnswer,
		cmd:       cmd,
		cancel:    procCancel,
	}
	session.lastHeartbeat.Store(time.Now().UnixMilli())

	sm.mu.Lock()
	sm.sessions[session.ID] = session
	sm.mu.Unlock()

	go sm.logProxyStderr(session.ID, stderr)
	go sm.watchSessionExit(session)

	log.Printf("started relay session id=%s camera_id=%s", session.ID, session.CameraID)
	return session, nil
}

func (sm *SessionManager) logProxyStderr(sessionID string, stderrPipe io.ReadCloser) {
	defer stderrPipe.Close()

	scanner := bufio.NewScanner(stderrPipe)
	for scanner.Scan() {
		log.Printf("ffmpeg session_id=%s %s", sessionID, scanner.Text())
	}
	if err := scanner.Err(); err != nil {
		log.Printf("ffmpeg stderr scanner error session_id=%s err=%v", sessionID, err)
	}
}

func (sm *SessionManager) watchSessionExit(session *RelaySession) {
	err := session.cmd.Wait()
	if err != nil {
		log.Printf("relay process exited with error session_id=%s err=%v", session.ID, err)
	}

	sm.mu.Lock()
	current, ok := sm.sessions[session.ID]
	if ok && current == session {
		delete(sm.sessions, session.ID)
	}
	sm.mu.Unlock()

	session.cancel()
	log.Printf("relay session ended id=%s", session.ID)
}

func (sm *SessionManager) TouchHeartbeat(sessionID string) bool {
	sm.mu.RLock()
	session, ok := sm.sessions[sessionID]
	sm.mu.RUnlock()
	if !ok {
		return false
	}
	session.lastHeartbeat.Store(time.Now().UnixMilli())
	return true
}

func (sm *SessionManager) StopSession(sessionID, reason string) bool {
	sm.mu.Lock()
	session, ok := sm.sessions[sessionID]
	if ok {
		delete(sm.sessions, sessionID)
	}
	sm.mu.Unlock()
	if !ok {
		return false
	}

	log.Printf("stopping relay session id=%s reason=%s", sessionID, reason)
	session.cancel()

	if session.cmd != nil && session.cmd.Process != nil {
		if err := session.cmd.Process.Signal(os.Interrupt); err != nil {
			log.Printf("session interrupt failed id=%s err=%v", sessionID, err)
		}
		time.AfterFunc(2*time.Second, func() {
			if killErr := session.cmd.Process.Kill(); killErr != nil {
				if !errors.Is(killErr, os.ErrProcessDone) {
					log.Printf("session kill failed id=%s err=%v", sessionID, killErr)
				}
			}
		})
	}

	return true
}

func (sm *SessionManager) StopAll() {
	sm.mu.RLock()
	ids := make([]string, 0, len(sm.sessions))
	for id := range sm.sessions {
		ids = append(ids, id)
	}
	sm.mu.RUnlock()

	for _, id := range ids {
		sm.StopSession(id, "service shutdown")
	}
}

func (sm *SessionManager) RunInactivityReaper(ctx context.Context) {
	ticker := time.NewTicker(5 * time.Second)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			nowMs := time.Now().UnixMilli()
			stale := make([]string, 0)

			sm.mu.RLock()
			for id, session := range sm.sessions {
				last := session.lastHeartbeat.Load()
				if time.Duration(nowMs-last)*time.Millisecond > sm.inactivity {
					stale = append(stale, id)
				}
			}
			sm.mu.RUnlock()

			for _, id := range stale {
				sm.StopSession(id, "inactivity timeout")
			}
		}
	}
}

func probeRTSP(conn net.Conn, rtspURL string) error {
	if err := conn.SetDeadline(time.Now().Add(3 * time.Second)); err != nil {
		return fmt.Errorf("set rtsp probe deadline: %w", err)
	}

	request := fmt.Sprintf("OPTIONS %s RTSP/1.0\r\nCSeq: 1\r\nUser-Agent: CityFibreRelay/1.0\r\n\r\n", rtspURL)
	if _, err := conn.Write([]byte(request)); err != nil {
		return fmt.Errorf("send rtsp OPTIONS: %w", err)
	}

	buf := make([]byte, 512)
	n, err := conn.Read(buf)
	if err != nil {
		return fmt.Errorf("read rtsp OPTIONS response: %w", err)
	}
	resp := string(buf[:n])
	if !strings.Contains(resp, "RTSP/1.0") {
		return fmt.Errorf("invalid rtsp probe response: %q", resp)
	}

	return nil
}

func rtspHostPort(rtspURL string) (string, error) {
	u, err := url.Parse(rtspURL)
	if err != nil {
		return "", fmt.Errorf("invalid rtsp_url: %w", err)
	}
	if u.Scheme != "rtsp" {
		return "", errors.New("rtsp_url must use rtsp scheme")
	}
	if u.Host == "" {
		return "", errors.New("rtsp_url host is required")
	}

	host := u.Hostname()
	port := u.Port()
	if host == "" {
		return "", errors.New("rtsp_url hostname is required")
	}
	if port == "" {
		port = "554"
	}
	return net.JoinHostPort(host, port), nil
}

func freeUDPPort() (int, error) {
	pc, err := net.ListenPacket("udp", "127.0.0.1:0")
	if err != nil {
		return 0, err
	}
	defer pc.Close()

	addr, ok := pc.LocalAddr().(*net.UDPAddr)
	if !ok {
		return 0, errors.New("failed to resolve local UDP addr")
	}
	return addr.Port, nil
}

func buildFFmpegCommand(ctx context.Context, rtspURL string, videoPort int) *exec.Cmd {
	output := fmt.Sprintf("rtp://127.0.0.1:%d?pkt_size=1200", videoPort)
	args := []string{
		"-hide_banner",
		"-loglevel", "error",
		"-nostdin",
		"-rtsp_transport", "tcp",
		"-fflags", "nobuffer",
		"-flags", "low_delay",
		"-i", rtspURL,
		"-an",
		"-c:v", "libx264",
		"-preset", "ultrafast",
		"-tune", "zerolatency",
		"-pix_fmt", "yuv420p",
		"-profile:v", "baseline",
		"-f", "rtp",
		"-payload_type", "96",
		output,
	}
	return exec.CommandContext(ctx, "ffmpeg", args...)
}

func buildSDPAnswer(offer string, rtpPort int) (string, error) {
	if strings.TrimSpace(offer) == "" {
		return "", errors.New("sdp_offer is required")
	}
	if !strings.Contains(offer, "v=0") {
		return "", errors.New("sdp_offer is malformed")
	}

	codec := "H264"
	if strings.Contains(strings.ToUpper(offer), "VP8/90000") {
		codec = "VP8"
	}

	iceUfrag, err := randomHex(4)
	if err != nil {
		return "", err
	}
	icePwd, err := randomHex(16)
	if err != nil {
		return "", err
	}

	fingerprint := strings.TrimSpace(os.Getenv("STREAM_DTLS_FINGERPRINT"))
	if fingerprint == "" {
		fingerprint = "AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99"
	}

	answer := strings.Join([]string{
		"v=0",
		fmt.Sprintf("o=- %d 2 IN IP4 127.0.0.1", time.Now().UnixMilli()),
		"s=CityFibre Relay",
		"t=0 0",
		"a=group:BUNDLE 0",
		"a=msid-semantic: WMS*",
		"m=video 9 UDP/TLS/RTP/SAVPF 96",
		"c=IN IP4 0.0.0.0",
		"a=mid:0",
		"a=rtcp-mux",
		"a=setup:active",
		"a=sendonly",
		fmt.Sprintf("a=rtpmap:96 %s/90000", codec),
		fmt.Sprintf("a=ice-ufrag:%s", iceUfrag),
		fmt.Sprintf("a=ice-pwd:%s", icePwd),
		fmt.Sprintf("a=fingerprint:sha-256 %s", fingerprint),
		fmt.Sprintf("a=candidate:1 1 udp 2130706431 127.0.0.1 %d typ host", rtpPort),
		"a=end-of-candidates",
	}, "\r\n") + "\r\n"

	return answer, nil
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

func buildServerTLSConfig(certFile, keyFile, caFile string) (*tls.Config, error) {
	cert, err := tls.LoadX509KeyPair(certFile, keyFile)
	if err != nil {
		return nil, fmt.Errorf("load server cert/key failed: %w", err)
	}

	caPEM, err := os.ReadFile(caFile)
	if err != nil {
		return nil, fmt.Errorf("read CA file failed: %w", err)
	}
	caPool := x509.NewCertPool()
	if ok := caPool.AppendCertsFromPEM(caPEM); !ok {
		return nil, errors.New("parse CA PEM failed")
	}

	return &tls.Config{
		MinVersion:   tls.VersionTLS12,
		Certificates: []tls.Certificate{cert},
		ClientAuth:   tls.RequireAndVerifyClientCert,
		ClientCAs:    caPool,
	}, nil
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

	h := hex.EncodeToString(b)
	return fmt.Sprintf("%s-%s-%s-%s-%s", h[0:8], h[8:12], h[12:16], h[16:20], h[20:32])
}

func randomHex(byteLen int) (string, error) {
	b := make([]byte, byteLen)
	if _, err := rand.Read(b); err != nil {
		return "", fmt.Errorf("crypto/rand failed: %w", err)
	}
	return hex.EncodeToString(b), nil
}

func main() {
	listenAddr := envOrDefault("STREAM_RELAY_LISTEN_ADDR", ":9443")

	certFile, err := requiredEnv("STREAM_RELAY_TLS_CERT_FILE")
	if err != nil {
		log.Fatalf("configuration error: %v", err)
	}
	keyFile, err := requiredEnv("STREAM_RELAY_TLS_KEY_FILE")
	if err != nil {
		log.Fatalf("configuration error: %v", err)
	}
	caFile, err := requiredEnv("STREAM_RELAY_TLS_CA_FILE")
	if err != nil {
		log.Fatalf("configuration error: %v", err)
	}

	tlsConfig, err := buildServerTLSConfig(certFile, keyFile, caFile)
	if err != nil {
		log.Fatalf("build TLS config failed: %v", err)
	}

	connPool := NewRTSPConnectionPool(4, 3*time.Second)
	defer connPool.Close()

	sessionManager := NewSessionManager(30*time.Second, connPool)
	reaperCtx, reaperCancel := context.WithCancel(context.Background())
	defer reaperCancel()
	go sessionManager.RunInactivityReaper(reaperCtx)

	gin.SetMode(gin.ReleaseMode)
	router := gin.New()
	router.Use(gin.Logger(), gin.Recovery())

	router.POST("/api/v1/streams/play", func(c *gin.Context) {
		if err := verifyMutualTLS(c.Request); err != nil {
			c.JSON(http.StatusUnauthorized, gin.H{"error": err.Error()})
			return
		}

		var req PlayRequest
		if err := c.ShouldBindJSON(&req); err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": fmt.Sprintf("invalid request JSON: %v", err)})
			return
		}

		req.CameraID = strings.TrimSpace(req.CameraID)
		req.RTSPURL = strings.TrimSpace(req.RTSPURL)
		req.SDPOffer = strings.TrimSpace(req.SDPOffer)

		if req.CameraID == "" {
			c.JSON(http.StatusBadRequest, gin.H{"error": "camera_id is required"})
			return
		}
		if req.RTSPURL == "" {
			c.JSON(http.StatusBadRequest, gin.H{"error": "rtsp_url is required"})
			return
		}
		if req.SDPOffer == "" {
			c.JSON(http.StatusBadRequest, gin.H{"error": "sdp_offer is required"})
			return
		}

		session, err := sessionManager.StartSession(c.Request.Context(), req)
		if err != nil {
			c.JSON(http.StatusBadGateway, gin.H{"error": err.Error()})
			return
		}

		c.JSON(http.StatusOK, gin.H{
			"sdp_answer": session.SDPAnswer,
			"session_id": session.ID,
		})
	})

	router.POST("/api/v1/streams/heartbeat/:session_id", func(c *gin.Context) {
		if err := verifyMutualTLS(c.Request); err != nil {
			c.JSON(http.StatusUnauthorized, gin.H{"error": err.Error()})
			return
		}

		sessionID := strings.TrimSpace(c.Param("session_id"))
		if sessionID == "" {
			c.JSON(http.StatusBadRequest, gin.H{"error": "session_id is required"})
			return
		}
		if ok := sessionManager.TouchHeartbeat(sessionID); !ok {
			c.JSON(http.StatusNotFound, gin.H{"error": "session not found"})
			return
		}

		c.JSON(http.StatusOK, gin.H{"status": "ALIVE", "session_id": sessionID})
	})

	srv := &http.Server{
		Addr:              listenAddr,
		Handler:           router,
		TLSConfig:         tlsConfig,
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       15 * time.Second,
		WriteTimeout:      15 * time.Second,
		IdleTimeout:       120 * time.Second,
		MaxHeaderBytes:    1 << 20,
	}

	listener, err := tls.Listen("tcp", listenAddr, tlsConfig)
	if err != nil {
		log.Fatalf("bind listener failed: %v", err)
	}

	serveErrCh := make(chan error, 1)
	go func() {
		if serveErr := srv.Serve(listener); serveErr != nil && !errors.Is(serveErr, http.ErrServerClosed) {
			serveErrCh <- serveErr
		}
	}()

	log.Printf("stream relay controller listening on %s", listenAddr)

	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)

	select {
	case sig := <-sigCh:
		log.Printf("shutdown signal received: %s", sig.String())
	case serveErr := <-serveErrCh:
		log.Printf("server terminated with error: %v", serveErr)
	}

	reaperCancel()
	sessionManager.StopAll()

	shutdownCtx, shutdownCancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer shutdownCancel()
	if err := srv.Shutdown(shutdownCtx); err != nil {
		log.Printf("http shutdown error: %v", err)
	}
}
