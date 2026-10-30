package main

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"net"
	"strconv"
	"time"
)

// brokerSettings is the broker that the owner explicitly saved and allowed.
type brokerSettings struct {
	Host, Username, Password string
	Port                     int
	TLS                      bool
}

type mqttMessage struct {
	Topic   string
	Payload []byte
}

// The local helper is the only MQTT publisher. Connections are numeric-only;
// every operation is separately authorized by the controller.
type mqttSession struct {
	conn     net.Conn
	packetID uint16
}

func mqttDeadline(ctx context.Context) time.Time {
	deadline := time.Now().Add(20 * time.Second)
	if d, ok := ctx.Deadline(); ok && d.Before(deadline) {
		deadline = d
	}
	return deadline
}

func openMQTT(ctx context.Context, b brokerSettings, clientID string, roots *x509.CertPool) (*mqttSession, error) {
	if ip := net.ParseIP(b.Host); ip == nil || ip.To4() == nil {
		return nil, errors.New("broker must be a numeric IPv4 address")
	}
	for _, s := range []string{clientID, b.Username, b.Password} {
		if len(s) > 65535 {
			return nil, errors.New("MQTT field is too large")
		}
	}
	conn, err := (&net.Dialer{Timeout: 10 * time.Second}).DialContext(ctx, "tcp4", net.JoinHostPort(b.Host, strconv.Itoa(b.Port)))
	if err != nil {
		return nil, fmt.Errorf("could not reach the broker: %w", err)
	}
	success := false
	defer func() {
		if !success {
			conn.Close()
		}
	}()
	rawConn := conn
	stop := context.AfterFunc(ctx, func() { rawConn.Close() })
	defer stop()
	conn.SetDeadline(mqttDeadline(ctx))
	if b.TLS {
		secure := tls.Client(conn, &tls.Config{MinVersion: tls.VersionTLS12, ServerName: b.Host, RootCAs: roots})
		if err = secure.HandshakeContext(ctx); err != nil {
			return nil, fmt.Errorf("broker TLS failed: %w", err)
		}
		conn = secure
	}
	connect := mqttString("MQTT")
	connect = append(connect, 4, 0xc2, 0, 30) // MQTT 3.1.1, authenticated clean session, 30 s keepalive
	for _, s := range []string{clientID, b.Username, b.Password} {
		connect = append(connect, mqttString(s)...)
	}
	if err = writePacket(conn, 0x10, connect); err != nil {
		return nil, err
	}
	kind, body, err := readPacket(conn)
	if err != nil {
		return nil, fmt.Errorf("broker did not answer the connection: %w", err)
	}
	if kind != 0x20 || len(body) != 2 || body[0] != 0 {
		return nil, errors.New("broker sent an unexpected connection reply")
	}
	if body[1] != 0 {
		reasons := map[byte]string{1: "unsupported protocol version", 2: "client ID refused", 3: "broker unavailable", 4: "wrong username or password", 5: "not authorized"}
		if reason, ok := reasons[body[1]]; ok {
			return nil, errors.New("broker refused the connection: " + reason)
		}
		return nil, fmt.Errorf("broker refused the connection (code %d)", body[1])
	}
	if err = ctx.Err(); err != nil {
		return nil, err
	}
	success = true
	return &mqttSession{conn: conn}, nil
}

func (s *mqttSession) close() {
	if s != nil && s.conn != nil {
		s.conn.Close()
	}
}

func (s *mqttSession) publish(ctx context.Context, messages []mqttMessage, retained bool) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	s.conn.SetDeadline(mqttDeadline(ctx))
	stop := context.AfterFunc(ctx, func() { s.close() })
	defer stop()
	for _, m := range messages {
		if len(m.Topic) == 0 || len(m.Topic) > 65535 {
			return errors.New("invalid MQTT topic length")
		}
		s.packetID++
		if s.packetID == 0 {
			s.packetID = 1
		}
		packet := mqttString(m.Topic)
		packet = binary.BigEndian.AppendUint16(packet, s.packetID)
		packet = append(packet, m.Payload...)
		header := byte(0x32) // QoS 1, current state is never retained
		if retained {
			header |= 1
		}
		if err := writePacket(s.conn, header, packet); err != nil {
			return err
		}
		kind, body, err := readPacket(s.conn)
		if err != nil {
			return fmt.Errorf("broker did not acknowledge the message: %w", err)
		}
		if kind != 0x40 || len(body) != 2 || binary.BigEndian.Uint16(body) != s.packetID {
			return errors.New("broker sent an unexpected acknowledgement")
		}
	}
	return nil
}

func (s *mqttSession) ping(ctx context.Context) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	s.conn.SetDeadline(mqttDeadline(ctx))
	stop := context.AfterFunc(ctx, func() { s.close() })
	defer stop()
	if err := writePacket(s.conn, 0xc0, nil); err != nil {
		return err
	}
	kind, body, err := readPacket(s.conn)
	if err != nil {
		return err
	}
	if kind != 0xd0 || len(body) != 0 {
		return errors.New("broker sent an unexpected keepalive reply")
	}
	return nil
}

// Discovery is retained; live readings use a separate persistent session.
func publishRetained(ctx context.Context, b brokerSettings, clientID string, roots *x509.CertPool, messages []mqttMessage) error {
	s, err := openMQTT(ctx, b, clientID, roots)
	if err != nil {
		return err
	}
	defer s.close()
	if err = s.publish(ctx, messages, true); err != nil {
		return err
	}
	return writePacket(s.conn, 0xe0, nil)
}

func mqttString(s string) []byte {
	return append(binary.BigEndian.AppendUint16(nil, uint16(len(s))), s...)
}

func writePacket(w io.Writer, header byte, body []byte) error {
	if len(body) > 256*1024 {
		return errors.New("MQTT message too large")
	}
	packet := []byte{header}
	for n := len(body); ; {
		digit := byte(n % 128)
		n /= 128
		if n > 0 {
			digit |= 0x80
		}
		packet = append(packet, digit)
		if n == 0 {
			break
		}
	}
	packet = append(packet, body...)
	n, e := w.Write(packet)
	if e == nil && n != len(packet) {
		return io.ErrShortWrite
	}
	return e
}

func readPacket(r io.Reader) (byte, []byte, error) {
	var b [1]byte
	if _, e := io.ReadFull(r, b[:]); e != nil {
		return 0, nil, e
	}
	header := b[0]
	length, shift := 0, 0
	for i := 0; ; i++ {
		if i == 4 {
			return 0, nil, errors.New("invalid MQTT length")
		}
		if _, e := io.ReadFull(r, b[:]); e != nil {
			return 0, nil, e
		}
		length |= int(b[0]&0x7f) << shift
		shift += 7
		if b[0]&0x80 == 0 {
			break
		}
	}
	if length > 64*1024 {
		return 0, nil, errors.New("MQTT reply too large")
	}
	body := make([]byte, length)
	_, e := io.ReadFull(r, body)
	return header, body, e
}
