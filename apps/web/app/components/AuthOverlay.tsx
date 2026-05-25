"use client";
import { useState, useEffect } from "react";

export default function AuthOverlay({ children }: { children: React.ReactNode }) {
  const [isAuth, setIsAuth] = useState(true); 
  const [loading, setLoading] = useState(true);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState(false);

  useEffect(() => {
    const auth = localStorage.getItem("auth_navneet");
    if (auth !== "true") {
      setIsAuth(false);
    }
    setLoading(false);
  }, []);

  function handleLogin(e: React.FormEvent) {
    e.preventDefault();
    if (username.trim() === "Navneet@6263" && password === "Navneet") {
      localStorage.setItem("auth_navneet", "true");
      setIsAuth(true);
      setError(false);
    } else {
      setError(true);
    }
  }

  // To prevent hydration mismatch, we don't render until loaded
  if (loading) return <div style={{ minHeight: "100vh", backgroundColor: "#0b0c10" }}></div>;

  if (isAuth) {
    return <>{children}</>;
  }

  return (
    <>
      <div style={{ filter: "blur(8px)", pointerEvents: "none", height: "100vh", overflow: "hidden" }}>
        {children}
      </div>
      <div style={{
        position: "fixed",
        top: 0, left: 0, right: 0, bottom: 0,
        display: "flex", alignItems: "center", justifyContent: "center",
        backgroundColor: "rgba(0, 0, 0, 0.5)",
        backdropFilter: "blur(4px)",
        zIndex: 9999
      }}>
        <form onSubmit={handleLogin} style={{
          backgroundColor: "#161b22", padding: "40px", borderRadius: "16px",
          display: "flex", flexDirection: "column", gap: "20px",
          width: "360px", border: "1px solid #30363d",
          boxShadow: "0 20px 40px rgba(0,0,0,0.8)"
        }}>
          <h2 style={{ color: "#fff", margin: 0, textAlign: "center", fontSize: "24px", letterSpacing: "1px" }}>Secure Access</h2>
          {error && <p style={{ color: "#f85149", fontSize: "14px", margin: 0, textAlign: "center", fontWeight: "bold" }}>Incorrect credentials</p>}
          
          <div style={{ display: "flex", flexDirection: "column", gap: "8px" }}>
            <label style={{ color: "#8b949e", fontSize: "13px" }}>Username</label>
            <input
              type="text"
              placeholder="Username"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              style={{ 
                padding: "12px", borderRadius: "8px", border: "1px solid #30363d", 
                backgroundColor: "#0d1117", color: "#fff", outline: "none", fontSize: "15px"
              }}
            />
          </div>

          <div style={{ display: "flex", flexDirection: "column", gap: "8px" }}>
            <label style={{ color: "#8b949e", fontSize: "13px" }}>Password</label>
            <input
              type="password"
              placeholder="Password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              style={{ 
                padding: "12px", borderRadius: "8px", border: "1px solid #30363d", 
                backgroundColor: "#0d1117", color: "#fff", outline: "none", fontSize: "15px"
              }}
            />
          </div>
          
          <button type="submit" style={{
            padding: "14px", borderRadius: "8px", backgroundColor: "#238636",
            color: "#fff", border: "none", cursor: "pointer", fontWeight: "600",
            fontSize: "15px", marginTop: "10px", transition: "background-color 0.2s"
          }}>
            Unlock Dashboard
          </button>
        </form>
      </div>
    </>
  );
}
