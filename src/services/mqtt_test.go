package main

import (
	"bytes"
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/binary"
	"fmt"
	"math/big"
	"net"
	"strconv"
	"strings"
	"testing"
	"time"
)

type brokerFixture struct {
	connackCode byte
	pubackID    uint16 // 0: acknowledge the received ID
	connect     []byte
	published   []mqttMessage
	retained    []bool
	err         error
}

// serve answers one MQTT 3.1.1 client and records what it sent.
func (f *brokerFixture) serve(conn net.Conn, done chan<- struct{}) {
	defer close(done)
	defer conn.Close()
	conn.SetDeadline(time.Now().Add(5 * time.Second))
	kind, body, err := readPacket(conn)
	if err != nil || kind != 0x10 {
		f.err = err
		return
	}
	f.connect = body
	writePacket(conn, 0x20, []byte{0, f.connackCode})
	for {
		kind, body, err = readPacket(conn)
		if err != nil || kind == 0xe0 {
			return
		}
		if kind == 0xc0 {
			writePacket(conn, 0xd0, nil)
			continue
		}
		if kind != 0x32 && kind != 0x33 {
			f.err = fmt.Errorf("unexpected MQTT packet %x", kind)
			return
		}
		topicLength := int(binary.BigEndian.Uint16(body))
		id := binary.BigEndian.Uint16(body[2+topicLength:])
		f.published = append(f.published, mqttMessage{Topic: string(body[2 : 2+topicLength]), Payload: body[4+topicLength:]})
		f.retained = append(f.retained, kind == 0x33)
		if f.pubackID != 0 {
			id = f.pubackID
		}
		writePacket(conn, 0x40, binary.BigEndian.AppendUint16(nil, id))
	}
}

func startBroker(t *testing.T, f *brokerFixture, config *tls.Config) (brokerSettings, chan struct{}) {
	t.Helper()
	listener, err := net.Listen("tcp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { listener.Close() })
	done := make(chan struct{})
	go func() {
		conn, err := listener.Accept()
		if err != nil {
			close(done)
			return
		}
		if config != nil {
			conn = tls.Server(conn, config)
		}
		f.serve(conn, done)
	}()
	port, _ := strconv.Atoi(strings.Split(listener.Addr().String(), ":")[1])
	return brokerSettings{Host: "127.0.0.1", Port: port, Username: "user", Password: "pässwörd", TLS: config != nil}, done
}

func TestPublishRetainedSendsAcknowledgedRetainedMessages(t *testing.T) {
	f := &brokerFixture{}
	broker, done := startBroker(t, f, nil)
	payload := bytes.Repeat([]byte("x"), 300) // Two-byte remaining length.
	messages := []mqttMessage{{"homeassistant/device/a/config", payload}, {"homeassistant/device/b/config", nil}}
	if err := publishRetained(context.Background(), broker, "cgs2-ha-test", nil, messages); err != nil {
		t.Fatal(err)
	}
	<-done
	want := append(mqttString("MQTT"), 4, 0xc2, 0, 30)
	for _, field := range []string{"cgs2-ha-test", "user", "pässwörd"} {
		want = append(want, mqttString(field)...)
	}
	if !bytes.Equal(f.connect, want) {
		t.Fatalf("connect packet %x, want %x", f.connect, want)
	}
	if len(f.published) != 2 || f.published[0].Topic != messages[0].Topic || !bytes.Equal(f.published[0].Payload, payload) ||
		len(f.published[1].Payload) != 0 || !f.retained[0] || !f.retained[1] {
		t.Fatalf("published %+v retained %v", f.published, f.retained)
	}
}

func TestLiveSessionPublishesOnlyNonRetainedCurrentMessagesAndPings(t *testing.T) {
	f := &brokerFixture{}
	b, done := startBroker(t, f, nil)
	s, err := openMQTT(context.Background(), b, "local-readings", nil)
	if err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 3; i++ {
		if err = s.publish(context.Background(), []mqttMessage{{"readings", []byte(`{"version":1,"temperature":22}`)}}, false); err != nil {
			t.Fatal(err)
		}
		if err = s.ping(context.Background()); err != nil {
			t.Fatal(err)
		}
	}
	s.close()
	<-done
	if f.err != nil || len(f.published) != 3 {
		t.Fatalf("%v: %d messages", f.err, len(f.published))
	}
	for _, retained := range f.retained {
		if retained {
			t.Fatal("live state must not be retained")
		}
	}
}

func TestPublishRetainedReportsRefusalsAndProtocolErrors(t *testing.T) {
	for name, f := range map[string]*brokerFixture{
		"wrong username or password": {connackCode: 4},
		"not authorized":             {connackCode: 5},
		"unexpected acknowledgement": {pubackID: 9},
	} {
		t.Run(name, func(t *testing.T) {
			broker, _ := startBroker(t, f, nil)
			err := publishRetained(context.Background(), broker, "id", nil, []mqttMessage{{"t", []byte("p")}})
			if err == nil || !strings.Contains(err.Error(), name) {
				t.Fatalf("got %v", err)
			}
		})
	}
}

func TestPublishRetainedRefusesImplicitDNS(t *testing.T) {
	broker, _ := startBroker(t, &brokerFixture{}, nil)
	broker.Host = "localhost"
	err := publishRetained(context.Background(), broker, "id", nil, nil)
	if err == nil || !strings.Contains(err.Error(), "numeric IPv4") {
		t.Fatalf("expected refusal before system DNS, got %v", err)
	}
}

func TestPublishRetainedVerifiesTheBrokerAddressCertificate(t *testing.T) {
	key, _ := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	for name, address := range map[string]string{"matching address": "127.0.0.1", "other address": "192.0.2.1"} {
		t.Run(name, func(t *testing.T) {
			template := &x509.Certificate{SerialNumber: big.NewInt(1), Subject: pkix.Name{CommonName: "broker"},
				NotBefore: time.Now().Add(-time.Hour), NotAfter: time.Now().Add(time.Hour), IsCA: true, BasicConstraintsValid: true,
				KeyUsage: x509.KeyUsageCertSign | x509.KeyUsageDigitalSignature, ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
				IPAddresses: []net.IP{net.ParseIP(address)}}
			der, _ := x509.CreateCertificate(rand.Reader, template, template, &key.PublicKey, key)
			certificate, _ := x509.ParseCertificate(der)
			roots := x509.NewCertPool()
			roots.AddCert(certificate)
			f := &brokerFixture{}
			broker, _ := startBroker(t, f, &tls.Config{Certificates: []tls.Certificate{{Certificate: [][]byte{der}, PrivateKey: key}}})
			err := publishRetained(context.Background(), broker, "id", roots, []mqttMessage{{"t", []byte("p")}})
			if (err == nil) != (address == "127.0.0.1") {
				t.Fatalf("got %v", err)
			}
		})
	}
}

func TestPacketLengthRoundTrip(t *testing.T) {
	for _, n := range []int{0, 127, 128, 16383, 16384, 64 * 1024} {
		var buffer bytes.Buffer
		if err := writePacket(&buffer, 0x30, make([]byte, n)); err != nil {
			t.Fatal(err)
		}
		kind, body, err := readPacket(&buffer)
		if err != nil || kind != 0x30 || len(body) != n {
			t.Fatalf("%d: kind %x length %d err %v", n, kind, len(body), err)
		}
	}
}
