# Como Hospedar o REWE Dashboard no Wix.com (anschau.eu)

## Opção 1: Embed HTML no Wix (Mais Simples)

### Passo 1: Preparar o arquivo
O `report.html` é um arquivo único e autossuficiente (sem dependências locais, apenas Chart.js via CDN). Pode ser hospedado diretamente.

### Passo 2: No Wix Editor
1. Acesse **wix.com** → Seu site (anschau.eu)
2. Adicione uma nova página: **"REWE Dashboard"**
3. Nas configurações da página:
   - **Permissões** → "Apenas membros" ou "Protegido por senha"
   - Defina a senha desejada
4. Na página, adicione o elemento **"Embed" → "HTML personalizado"**
5. Cole o conteúdo do `report.html` no iframe HTML
6. Redimensione o iframe para ocupar toda a página (largura 100%, altura 100vh)

**Limitação:** O Wix limita o tamanho do HTML embed (~1MB). O report.html tem ~750KB, deve funcionar.

### Passo 3: Configurar senha na página
No Wix Editor:
- Clique na página → **Configurações de página** → **Permissões**
- Selecione **"Protegido por senha"**
- Defina sua senha

---

## Opção 2: Velo by Wix (Para Updates Automáticos)

Se quiser atualizar o dashboard automaticamente quando novos recibos forem adicionados:

### Passo 1: Criar uma API no Wix Velo
No Wix Editor, ative o **Modo Desenvolvedor** (Dev Mode).

Crie o arquivo `backend/dashboard.jsw`:

```javascript
import wixData from 'wix-data';

export function updateDashboard(htmlContent) {
    return wixData.save("DashboardData", {
        _id: "main",
        html: htmlContent,
        updatedAt: new Date()
    });
}

export function getDashboard() {
    return wixData.get("DashboardData", "main");
}
```

### Passo 2: Criar a página com Velo

```javascript
import { getDashboard } from 'backend/dashboard.jsw';
import wixUsers from 'wix-users';

$w.onReady(async function () {
    // Check if user is logged in
    if (!wixUsers.currentUser.loggedIn) {
        wixUsers.promptLogin();
        return;
    }

    const data = await getDashboard();
    if (data && data.html) {
        $w('#htmlFrame').postMessage(data.html);
    }
});
```

---

## Opção 3: Hospedagem Estática com Senha (Recomendada)

A forma mais simples e robusta é hospedar os arquivos separadamente e usar o Wix apenas como redirect.

### Usar o `login.html` incluído nesta pasta

1. Faça upload do `login.html` e `report.html` para qualquer hospedagem estática:
   - **GitHub Pages** (gratuito): Crie um repo privado, ative Pages
   - **Netlify** (gratuito): Drag & drop dos arquivos
   - **Cloudflare Pages** (gratuito): Conecte ao GitHub
   - **Vercel** (gratuito): Deploy automático

2. No seu domínio `anschau.eu`:
   - Configure um subdomínio: `rewe.anschau.eu`
   - Aponte para a hospedagem escolhida
   - Ou no Wix, adicione um redirect: `anschau.eu/rewe` → URL da hospedagem

### Netlify (mais fácil):
```bash
# Instale netlify-cli
npm install -g netlify-cli

# Na pasta wix-hosting:
netlify deploy --prod --dir=.

# Configure senha no Netlify:
# Site settings → Access control → Password protection
```

---

## Atualizar o Dashboard

Sempre que tiver novos eBons:

```bash
# 1. Copie os PDFs para a pasta pdfs/
cp ~/Downloads/REWE-ebon*.pdf ~/Documents/Claude/Rewe/pdfs/

# 2. Execute o parser e o gerador
cd ~/Documents/Claude/Rewe
python3 rewe_parser.py && python3 build_report.py

# 3. Copie o report.html atualizado para a hospedagem
cp report.html wix-hosting/
```

---

## Upload de PDFs pelo Dashboard

O botão de upload no dashboard permite selecionar PDFs, mas como é um arquivo estático (HTML puro), ele não pode processá-los automaticamente no servidor.

**Fluxo recomendado:**
1. Use o dashboard para consultar dados
2. Para adicionar novos recibos, rode os comandos acima no terminal
3. O dashboard será atualizado automaticamente
