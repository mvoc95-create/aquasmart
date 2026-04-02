# Sistema Fazenda Aqua Smart

Sistema web para operação bifásica de camarão, pensado para uso em tablet, celular ou computador. Esta versão já vem com **login, perfis de acesso e permissões por usuário**.

## O que este sistema já faz
- Painel operacional do dia
- Semáforo por unidade
- Cadastro de lotes
- Lançamento de monitoramento da água
- Lançamento de manejo diário
- Transferências berçário -> engorda
- Estoque de ração
- Despesca e vendas
- Banco local SQLite ou banco remoto PostgreSQL/Supabase
- Login com senha
- Perfis de acesso por usuário
- Tela para criar, editar, ativar e desativar usuários

## Perfis de acesso
O sistema trabalha com 4 perfis:

### 1) Administrador
Pode acessar tudo, inclusive a tela de usuários.

### 2) Gerente
Pode operar o sistema inteiro, mas não pode gerenciar usuários.

### 3) Operador
Pode lançar água, manejo, transferências e ração.

### 4) Consulta
Pode apenas visualizar o painel e a aba de unidades.

## Usuário padrão na primeira execução
Na primeira vez que o sistema sobe, ele cria um usuário administrador automaticamente.

### Login padrão
- **Usuário:** `admin@fazendaaquasmart.local`
- **Senha:** `admin123`

## Muito importante
Entre com esse usuário assim que instalar e crie os usuários reais da fazenda.
Depois disso, você pode parar de usar o login padrão.

Se quiser trocar esse administrador padrão antes mesmo de rodar o sistema, use estas variáveis de ambiente:
- `ADMIN_NAME`
- `ADMIN_EMAIL`
- `ADMIN_PASSWORD`

Exemplo:

```bash
ADMIN_NAME="Matheus" \
ADMIN_EMAIL="matheus@empresa.com" \
ADMIN_PASSWORD="SuaSenhaForte123" \
python app.py
```

No Windows PowerShell:

```powershell
$env:ADMIN_NAME="Matheus"
$env:ADMIN_EMAIL="matheus@empresa.com"
$env:ADMIN_PASSWORD="SuaSenhaForte123"
python app.py
```

---

## Antes de começar
Você não precisa ser programador para colocar o sistema para funcionar, mas precisa seguir os passos com calma.

Você vai precisar de:
- um computador com Windows, macOS ou Linux para a instalação inicial
- internet para baixar os arquivos
- **Python 3.11 ou 3.12** instalado
- opcionalmente, uma conta no GitHub e um serviço de hospedagem para publicar online

> Observação: eu recomendo Python 3.11 ou 3.12 porque costuma dar menos dor de cabeça com dependências de banco remoto.

## Estrutura dos arquivos
- `app.py`: arquivo principal do sistema
- `requirements.txt`: lista de dependências
- `templates/`: telas do sistema
- `static/style.css`: aparência do sistema
- `instance/`: onde fica o banco SQLite local, se você optar pelo modo local

---

## Instalação local passo a passo

### 1) Baixe e extraia os arquivos
Coloque a pasta do sistema em um local fácil de achar, como a Área de Trabalho.

### 2) Instale o Python
Baixe e instale o Python pelo site oficial.
No Windows, marque a opção **Add Python to PATH** durante a instalação.

### 3) Abra o terminal na pasta do sistema
No Windows, você pode abrir o Prompt de Comando ou o PowerShell dentro da pasta.
No macOS e Linux, use o Terminal.

### 4) Crie o ambiente virtual
No terminal, execute:

```bash
python -m venv .venv
```

### 5) Ative o ambiente virtual

#### Windows
```bash
.venv\Scripts\activate
```

#### macOS / Linux
```bash
source .venv/bin/activate
```

Quando estiver ativo, o terminal normalmente mostra algo parecido com `(.venv)` no começo da linha.

### 6) Instale as dependências
```bash
pip install -r requirements.txt
```

### 7) Rode o sistema
```bash
python app.py
```

### 8) Abra no navegador
Entre em:

```text
http://localhost:8000
```

### 9) Faça login
Use o usuário padrão informado acima ou o administrador definido nas variáveis de ambiente.

---

## Onde ficam os dados quando roda localmente
Se você não configurar banco remoto, o sistema cria automaticamente um banco SQLite.

Normalmente ele fica em:

```text
instance/farm_system.db
```

Isso significa que:
- os dados ficam salvos naquele computador
- se você trocar de computador, precisa levar esse arquivo junto
- se o computador quebrar e não houver backup, você perde os dados

## Como fazer backup local
Feche o sistema e copie o arquivo `instance/farm_system.db` para:
- Google Drive
- OneDrive
- pendrive
- outro computador

---

## Como usar banco remoto
O sistema aceita `DATABASE_URL` com PostgreSQL, inclusive Supabase.

Exemplo:

```bash
DATABASE_URL=postgresql://usuario:senha@host:5432/postgres
```

No Windows PowerShell, antes de rodar:

```powershell
$env:DATABASE_URL="postgresql://usuario:senha@host:5432/postgres"
python app.py
```

No macOS/Linux:

```bash
export DATABASE_URL="postgresql://usuario:senha@host:5432/postgres"
python app.py
```

---

## Como rodar em produção
Para publicar online, o recomendado é usar Gunicorn.

Com ele, o comando de produção fica:

```bash
gunicorn app:app
```

## Variáveis importantes
- `SECRET_KEY`: chave de segurança do Flask
- `DATABASE_URL`: endereço do banco de dados
- `TARGET_NURSERY_DAYS`: dias-alvo do berçário
- `TARGET_OD_MIN`: OD mínimo
- `TARGET_PH_MIN`: pH mínimo
- `TARGET_PH_MAX`: pH máximo
- `TARGET_TEMP_MIN`: temperatura mínima
- `TARGET_TEMP_MAX`: temperatura máxima
- `ADMIN_NAME`: nome do primeiro administrador
- `ADMIN_EMAIL`: login do primeiro administrador
- `ADMIN_PASSWORD`: senha do primeiro administrador

---

## O que mudou nesta versão
- login obrigatório para entrar no sistema
- controle de sessão
- perfis diferentes por usuário
- permissões separadas por papel
- tela para criar e gerenciar acessos
- bloqueio automático de telas sem permissão
- logout seguro para uso em tablet ou computador compartilhado

---

## Problemas comuns

### "Usuário ou senha inválidos"
Confira se digitou o login corretamente.
Se for o primeiro acesso, teste:
- usuário: `admin@fazendaaquasmart.local`
- senha: `admin123`

### "Acesso negado"
Seu usuário entrou no sistema, mas o perfil dele não tem permissão para aquela área.
Entre com um administrador ou peça para o administrador ajustar seu perfil.

### "python não é reconhecido"
O Python não foi instalado corretamente ou não foi adicionado ao PATH. Reinstale e marque a opção de PATH.

### "pip não é reconhecido"
Mesmo problema acima. Normalmente é resolvido reinstalando o Python corretamente.

### O navegador não abre o sistema
Confira se o terminal mostrou erro. Se estiver tudo certo, acesse manualmente `http://localhost:8000`.

### Erro de banco remoto
Confira:
- usuário
- senha
- host
- porta
- nome do banco
- se a string começa com `postgresql://`

---

## Próximos passos recomendados
- publicar online em Render, Railway ou VPS
- usar Supabase como banco remoto
- trocar o usuário administrador padrão
- configurar backups automáticos
- adicionar HTTPS e domínio próprio

## Observação importante
O sistema já cria as tabelas automaticamente na primeira execução.
Se já existir banco antigo, ele tenta manter os dados e criar o que estiver faltando.
