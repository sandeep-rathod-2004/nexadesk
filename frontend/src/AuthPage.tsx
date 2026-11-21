import { FormEvent, useState } from "react";
import { Sparkles } from "lucide-react";
import {
  ApiError, AuthResult, LoginInstrumentation, RegistrationInstrumentation, login, register
} from "./lib/api";
import { recordFrontendPerformance } from "./performance";

export type AuthPageRoute = "login" | "register" | "forgot-password" | "reset-password";

type Props = {
  page: AuthPageRoute;
  onNavigate: (page: AuthPageRoute) => void;
  onAuthenticated: (result: AuthResult, instrumentation?: LoginInstrumentation) => void;
  authMessage?: string;
  onBackToLanding?: () => void;
};

const pageTitles: Record<AuthPageRoute, string> = {
  login: "Welcome back",
  register: "Create your account",
  "forgot-password": "Forgot password?",
  "reset-password": "Reset password"
};

export default function AuthPage({
  page,
  onNavigate,
  onAuthenticated,
  authMessage = "",
  onBackToLanding
}: Props) {
  const [fullName, setFullName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [workspaceName, setWorkspaceName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError("");
    setNotice("");

    if (page === "register" && password !== confirmPassword) {
      setError("Passwords do not match.");
      return;
    }

    setBusy(true);
    let loginInstrumentation: LoginInstrumentation | undefined;
    let registrationInstrumentation: RegistrationInstrumentation | undefined;
    if (page === "login") {
      try {
        loginInstrumentation = {
          requestId: crypto.randomUUID(),
          submittedAt: performance.now()
        };
      } catch {
        loginInstrumentation = undefined;
      }
    }
    if (page === "register") {
      try {
        registrationInstrumentation = {
          requestId: crypto.randomUUID(),
          submittedAt: performance.now()
        };
        recordFrontendPerformance(
          "registration_submission_started",
          registrationInstrumentation.submittedAt,
          { request_id: registrationInstrumentation.requestId }
        );
      } catch {
        registrationInstrumentation = undefined;
      }
    }
    if (loginInstrumentation) {
      recordFrontendPerformance(
        "login_submission_started",
        undefined,
        { request_id: loginInstrumentation.requestId }
      );
    }
    try {
      if (page === "register") {
        await register(
          fullName.trim(),
          email.trim(),
          password,
          workspaceName.trim() || "My Workspace",
          registrationInstrumentation
        );
        setPassword("");
        setConfirmPassword("");
        setNotice("Your account was created. Sign in with your new credentials.");
        onNavigate("login");
      } else {
        const result = await login(email.trim(), password, loginInstrumentation);
        onAuthenticated(result, loginInstrumentation);
      }
    } catch (requestError) {
      setError(
        requestError instanceof ApiError
          ? requestError.message
          : requestError instanceof Error
            ? requestError.message
            : "Authentication failed. Please try again."
      );
    } finally {
      setBusy(false);
    }
  }

  const recoveryPage = page === "forgot-password" || page === "reset-password";

  return <main className="auth-page">
    <section className="auth-card" aria-labelledby="auth-title">
      <div className="brand-row auth-brand">
        <div className="brand-mark"><Sparkles size={19}/></div>
        <span className="brand-name">nexa<span>desk</span></span>
      </div>
      <h1 id="auth-title">{pageTitles[page]}</h1>
      {recoveryPage ? <>
        <p>Password recovery is not available yet. No reset email or password change will be attempted.</p>
        <div className="auth-notice" role="status">
          The recovery service is not configured. You can return to Login.
        </div>
        {page === "forgot-password" ? <form onSubmit={event => event.preventDefault()}>
          <label>Email<input type="email" autoComplete="email" value={email} onChange={event => setEmail(event.target.value)} /></label>
          <button className="auth-submit" type="button" disabled>Recovery unavailable</button>
        </form> : <form onSubmit={event => event.preventDefault()}>
          <label>New password<input type="password" autoComplete="new-password" value={password} onChange={event => setPassword(event.target.value)} /></label>
          <label>Confirm new password<input type="password" autoComplete="new-password" value={confirmPassword} onChange={event => setConfirmPassword(event.target.value)} /></label>
          <button className="auth-submit" type="button" disabled>Reset unavailable</button>
        </form>}
        <button className="auth-toggle" type="button" onClick={() => onNavigate("login")}>Back to Login</button>
      </> : <form onSubmit={submit}>
        <p>{page === "register"
          ? "Create an account to start using your NexaDesk workspace."
          : "Sign in to securely access your conversations."}</p>
        {page === "register" && <>
          <label>Full name<input autoComplete="name" maxLength={120} required value={fullName} onChange={event => setFullName(event.target.value)} /></label>
          <label>Workspace name<input autoComplete="organization" maxLength={100} value={workspaceName} onChange={event => setWorkspaceName(event.target.value)} placeholder="My Workspace" /></label>
        </>}
        <label>Email<input type="email" autoComplete="email" required value={email} onChange={event => setEmail(event.target.value)} /></label>
        <label>Password<input type="password" autoComplete={page === "register" ? "new-password" : "current-password"} minLength={page === "register" ? 12 : 1} maxLength={128} required value={password} onChange={event => setPassword(event.target.value)} /></label>
        {page === "register" && <label>Confirm password<input type="password" autoComplete="new-password" minLength={12} maxLength={128} required value={confirmPassword} onChange={event => setConfirmPassword(event.target.value)} /></label>}
        {onBackToLanding && <button className="auth-toggle" type="button" onClick={onBackToLanding}>Back to NexaDesk</button>}
        {notice && <p className="auth-success" role="status">{notice}</p>}
        {(error || authMessage) && <p className="auth-error" role="alert">{error || authMessage}</p>}
        <button className="auth-submit" type="submit" disabled={busy}>
          {busy ? "Please wait…" : page === "register" ? "Create account" : "Sign in"}
        </button>
        {page === "login"
          ? <div className="auth-links">
              <button className="auth-toggle" type="button" onClick={() => onNavigate("register")}>Create an account</button>
              <button className="auth-toggle" type="button" onClick={() => onNavigate("forgot-password")}>Forgot Password?</button>
            </div>
          : <button className="auth-toggle" type="button" onClick={() => onNavigate("login")}>Already registered? Sign in</button>}
      </form>}
    </section>
  </main>;
}
