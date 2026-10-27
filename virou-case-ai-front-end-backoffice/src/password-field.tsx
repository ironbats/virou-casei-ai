import { useState } from "react";
import { Copy, Eye, EyeOff, WandSparkles } from "lucide-react";

const groups = [
  "ABCDEFGHJKLMNPQRSTUVWXYZ",
  "abcdefghijkmnopqrstuvwxyz",
  "23456789",
  "!@#$%*-_+?",
];
function randomIndex(size: number) {
  const bytes = new Uint32Array(1),
    limit = Math.floor(0x100000000 / size) * size;
  do {
    crypto.getRandomValues(bytes);
  } while (bytes[0] >= limit);
  return bytes[0] % size;
}
export function generatePassword() {
  const alphabet = groups.join("");
  const characters = groups.map((group) => group[randomIndex(group.length)]);
  while (characters.length < 24)
    characters.push(alphabet[randomIndex(alphabet.length)]);
  for (let i = characters.length - 1; i > 0; i--) {
    const j = randomIndex(i + 1);
    [characters[i], characters[j]] = [characters[j], characters[i]];
  }
  return characters.join("");
}
export function PasswordField({ initial = "" }: { initial?: string }) {
  const [value, setValue] = useState(initial),
    [visible, setVisible] = useState(false),
    [message, setMessage] = useState("");
  return (
    <div className="password-field">
      <label htmlFor="account-password">Senha inicial / nova senha</label>
      <div className="password-input">
        <input
          id="account-password"
          name="password"
          type={visible ? "text" : "password"}
          value={value}
          onChange={(e) => {
            setValue(e.target.value);
            setMessage("");
          }}
          autoComplete="new-password"
          required
          minLength={12}
          maxLength={128}
        />
        <button
          className="icon-button"
          type="button"
          aria-label={visible ? "Ocultar senha" : "Mostrar senha"}
          onClick={() => setVisible(!visible)}
        >
          {visible ? <EyeOff size={18} /> : <Eye size={18} />}
        </button>
      </div>
      <div className="actions">
        <button
          className="button"
          type="button"
          onClick={() => {
            setValue(generatePassword());
            setVisible(true);
            setMessage(
              "Senha de 24 caracteres gerada. Copie antes de concluir o cadastro.",
            );
          }}
        >
          <WandSparkles size={16} />
          Gerar senha
        </button>
        <button
          className="button"
          type="button"
          disabled={!value}
          onClick={async () => {
            try {
              await navigator.clipboard.writeText(value);
              setMessage("Senha copiada.");
            } catch {
              setVisible(true);
              setMessage("Selecione e copie a senha no campo acima.");
            }
          }}
        >
          <Copy size={16} />
          Copiar senha
        </button>
      </div>
      <small role="status">
        {message ||
          "Use pelo menos 12 caracteres. A senha não será exibida novamente após o cadastro."}
      </small>
    </div>
  );
}
