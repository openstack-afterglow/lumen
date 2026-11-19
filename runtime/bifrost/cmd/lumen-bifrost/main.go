package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"os"
	"os/signal"
	"runtime"
	"strconv"
	"syscall"
	"time"

	bifrost "github.com/openstack-afterglow/lumen/runtime/bifrost"
)

func main() {
	listen := flag.String("listen", "127.0.0.1:0", "private listen address")
	allowPrivate := flag.Bool("allow-private-network", false, "allow binding a non-loopback private address")
	version := flag.Bool("version", false, "report pinned Bifrost source version")
	flag.Parse()
	if *version {
		fmt.Printf("lumen-bifrost %s %s/%s\n", bifrost.Version, runtime.GOOS, runtime.GOARCH)
		return
	}
	if err := run(*listen, *allowPrivate); err != nil {
		fmt.Fprintln(os.Stderr, "lumen-bifrost: runtime failed")
		os.Exit(1)
	}
}
func run(address string, allowPrivate bool) error {
	host, _, err := net.SplitHostPort(address)
	if err != nil {
		return err
	}
	ip := net.ParseIP(host)
	if ip == nil || (!ip.IsLoopback() && !(allowPrivate && ip.IsPrivate())) {
		return fmt.Errorf("private bind required")
	}
	handler, err := bifrost.NewServer(os.Getenv("LUMEN_BIFROST_TOKEN"))
	if err != nil {
		return err
	}
	listener, err := net.Listen("tcp", address)
	if err != nil {
		return err
	}
	// Standard server diagnostics may contain request URLs. Suppress them rather than
	// leaking provider body, key, URL query or account material through a panic path.
	server := &http.Server{Handler: handler, ReadHeaderTimeout: 10 * time.Second, IdleTimeout: 30 * time.Second, MaxHeaderBytes: 64 << 10, ErrorLog: log.New(io.Discard, "", 0)}
	exited := make(chan error, 1)
	go func() { exited <- server.Serve(listener) }()
	if err = json.NewEncoder(os.Stdout).Encode(map[string]any{"ready": true, "address": listener.Addr().String(), "contract": bifrost.Contract, "version": bifrost.Version}); err != nil {
		_ = server.Close()
		return err
	}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()
	parentGone := make(chan struct{})
	if configured := os.Getenv("LUMEN_BIFROST_PARENT_PID"); configured != "" {
		parent, parseErr := strconv.Atoi(configured)
		if parseErr != nil || parent <= 0 {
			_ = server.Close()
			return fmt.Errorf("invalid managed parent")
		}
		go func() {
			ticker := time.NewTicker(time.Second)
			defer ticker.Stop()
			for {
				select {
				case <-ctx.Done():
					return
				case <-ticker.C:
					if os.Getppid() != parent {
						close(parentGone)
						return
					}
				}
			}
		}()
	}
	select {
	case err = <-exited:
		if err == http.ErrServerClosed {
			return nil
		}
		return err
	case <-ctx.Done():
	case <-parentGone:
	}
	handler.Drain()
	drain, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	if err = server.Shutdown(drain); err != nil {
		_ = server.Close()
		return err
	}
	return nil
}
