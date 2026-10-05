package backend

import (
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"strings"
	"time"
)

const saDir = "/var/run/secrets/kubernetes.io/serviceaccount/"

// sliceClient lists ready endpoints straight from the Kubernetes API. Unlike
// DNS (CoreDNS caches records for 30s) a deleted pod disappears from its
// EndpointSlice within about a second.
type sliceClient struct {
	http  *http.Client
	token string
	ns    string
}

func newSliceClient() *sliceClient {
	tok, err := os.ReadFile(saDir + "token")
	if err != nil {
		return nil // not in a cluster: DNS discovery only
	}
	ca, _ := os.ReadFile(saDir + "ca.crt")
	pool := x509.NewCertPool()
	pool.AppendCertsFromPEM(ca)
	ns, _ := os.ReadFile(saDir + "namespace")
	return &sliceClient{
		http:  &http.Client{Timeout: 2 * time.Second, Transport: &http.Transport{TLSClientConfig: &tls.Config{RootCAs: pool}}},
		token: strings.TrimSpace(string(tok)),
		ns:    strings.TrimSpace(string(ns)),
	}
}

// ready returns host:port of every ready endpoint behind a Service.
func (c *sliceClient) ready(service string, port string) ([]string, error) {
	u := fmt.Sprintf("https://kubernetes.default.svc/apis/discovery.k8s.io/v1/namespaces/%s/endpointslices?labelSelector=%s",
		c.ns, url.QueryEscape("kubernetes.io/service-name="+service))
	req, _ := http.NewRequest("GET", u, nil)
	req.Header.Set("Authorization", "Bearer "+c.token)
	resp, err := c.http.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("endpointslices %s: %d", service, resp.StatusCode)
	}
	var list struct {
		Items []struct {
			Ports []struct {
				Port *int `json:"port"`
			} `json:"ports"`
			Endpoints []struct {
				Addresses  []string `json:"addresses"`
				Conditions struct {
					Ready *bool `json:"ready"`
				} `json:"conditions"`
			} `json:"endpoints"`
		} `json:"items"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&list); err != nil {
		return nil, err
	}
	var out []string
	for _, it := range list.Items {
		p := port
		if len(it.Ports) > 0 && it.Ports[0].Port != nil {
			p = strconv.Itoa(*it.Ports[0].Port)
		}
		for _, ep := range it.Endpoints {
			if ep.Conditions.Ready != nil && !*ep.Conditions.Ready {
				continue
			}
			for _, a := range ep.Addresses {
				out = append(out, net.JoinHostPort(a, p))
			}
		}
	}
	return out, nil
}
