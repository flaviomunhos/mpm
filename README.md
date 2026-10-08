# MPM — Munhos PC Migrator

Ferramenta de migração de PCs Windows (ex.: Windows 10 → Windows 11), portátil e sem servidor central: leva o
**perfil e os arquivos**, **instala os programas** (winget) e copia **configurações e Wi-Fi** do PC antigo para o novo,
pela rede, com cópia verificada por SHA-256. Uma alternativa simples e transparente a ferramentas como o PCmover.

> **English:** MPM is a portable, network-based Windows PC migration tool (profile + files, programs via winget,
> app settings and saved Wi-Fi). The old PC (TX) is read-only; the new PC (RX) controls and writes. Python 3.12+,
> TLS 1.3 with a pairing code, SHA-256 verified copies. Documentation below is in Brazilian Portuguese.

Status: **0.5.12** (modo pessoal `--meu`; o `--full` da 0.5.11) — fluxo completo (`--full`) validado em campo: criação do usuário e do perfil (26,5 GB, ~40 mil
arquivos, 0 falhas), instalação de programas, configurações, registro e Wi-Fi; `mpm.exe` portátil para Windows;
log de cada execução. Veja a [checklist](#roadmap) no fim.

Uso rápido (no Windows, como administrador):

```text
# PC NOVO (RX): cria o usuário e traz tudo
mpm.exe rx --as-user NOME --admin --full
# PC ANTIGO (TX): ele acha o RX sozinho e pede o código mostrado no RX
mpm.exe tx
```

Sem argumentos, o `mpm.exe` abre um menu (ANTIGO / NOVO).

## Licença e aviso

Licenciado sob a [PolyForm Noncommercial 1.0.0](LICENSE): uso, cópia e modificação livres para fins **não comerciais**
(pessoal, estudo, pesquisa, ONGs etc.); uso comercial, inclusive venda, exige autorização do autor. Use **somente em computadores seus ou com autorização de quem os
administra**: a ferramenta lê perfis de outros usuários, cria contas e lê senhas de Wi-Fi salvas (mediante
administrador). O software é fornecido "como está", sem garantias; **teste antes** com dados que você possa perder e
mantenha backup do PC antigo (o TX só lê, mas o RX grava no destino).

## Requisitos

- Python 3.12+
- `pip install cryptography` (somente para o modo de rede `rx`/`tx`; o Discovery não precisa)

## Escopo (decisões do projeto)

- **Objetivo:** levar o perfil do usuário selecionado + os programas instalados.
- **Pastas copiadas:** Desktop, Documents (Documentos) e Pictures (Imagens). Downloads, Music e Videos
  ficam de fora. Em disco as pastas se chamam Documents/Pictures; os nomes em pt-BR são só de exibição.
- **AppData:** entra no Discovery como inventário. A migração será por aplicativo (plugins), em módulo próprio.
- **Programas instalados:** Application Discovery e instalação (winget com internet no RX, mais instaladores
  de uma pasta da rede como complemento).
- **Transporte principal:** rede. USB/offline fica como alternativa futura sobre a mesma camada de arquivos.
- **Testes de campo:** somente em máquinas fora de domínio.

## Migração pela rede

```text
   TX (máquina ANTIGA)                         RX (máquina NOVA)
   somente leitura        ── TLS 1.3 ──►       escuta, decide, escreve
   conecta ao RX                               controla todo o processo
```

No **RX** (máquina nova):

```powershell
python -m mpm rx --dest D:\Migracao --exclude node_modules --exclude *.iso
```

Ele mostra um **código de pareamento** e os endereços. No **TX** (máquina antiga, PowerShell elevado
para ler o perfil de outro usuário), **sem informar IP**:

```powershell
python -m mpm tx
```

O TX procura o RX na rede local (broadcast UDP, porta 47800):

- **1 RX encontrado:** mostra o nome do computador e pede o código.
- **Vários RX:** mostra uma lista numerada pelo nome do computador; você escolhe e digita o código
  exibido naquele RX (`r` procura de novo, `q` sai).
- **Nenhum:** ENTER procura de novo, ou digite `IP[:porta]` do RX.
- **Código errado:** pede de novo (3 tentativas, o mesmo limite do RX).

Atalho/alternativa manual, sem descoberta: `python -m mpm tx --rx 192.168.0.10 --code ABCD-EFGH`.

O RX pergunta qual perfil do TX migrar (ou use `--source-user NOME`), mostra o **plano** e pede confirmação.

| Opção do `rx` | Efeito |
|---|---|
| `--dest PASTA` | destino (obrigatório) |
| `--source-user NOME` | perfil do TX a migrar (nome ou SID) |
| `--exclude PADRÃO` | exclui por nome/caminho (`node_modules`, `*.iso`); repetível |
| `--passes N` | 1 = só massa; 2 (padrão) = massa + diferenças |
| `--threads N` | conexões de cópia em paralelo, como o `/MT` do Robocopy (1 a 32; padrão 8) |
| `--retries N` | tentativas extras por arquivo com falha transitória (padrão 2), como o `/R` |
| `--retry-wait SEG` | espera entre tentativas (padrão 2 s), como o `/W` |
| `--netcheck` | só mede a rede (latência e vazão com 1 e N conexões); não copia nada, dispensa `--dest` |
| `--dry-run` | mostra o plano e o espaço necessário; não copia nada |
| `-y` | sem perguntas (sem confirmação nem pausa entre passadas) |
| `--port N` | porta (padrão 47800) |

**Duas passadas:** a primeira copia em massa enquanto o TX está em uso. O RX então pausa; você fecha os
programas no TX e pressiona ENTER para a passada final, que copia só o que mudou ou apareceu.
Arquivos que mudam durante a leitura são descartados e ficam para a passada seguinte.

**Retomada:** rodar o `rx` de novo com o mesmo destino pula o que já está igual (tamanho e data).
Um arquivo interrompido no meio continua de onde parou (o `.mpm-part` é reaproveitado, como o `/Z` do
Robocopy); o SHA-256 final cobre o arquivo inteiro, então um parcial estragado nunca vira cópia válida.

### Cópia em paralelo (como o /MT do Robocopy)

Cada arquivo exige uma ida e volta na rede; em Wi-Fi ou com latência alta isso, e não a banda, limita a
velocidade. Por isso o RX pede ao TX `--threads` conexões extras e copia vários arquivos ao mesmo tempo.
Medido com latência simulada de 40 ms (400 arquivos de 8 KB + 4 de 8 MB): 1 conexão 2,0 MB/s, 4 conexões
7,8 MB/s, 8 conexões 15,3 MB/s, 16 conexões 28,8 MB/s. Em disco mecânico (HDD) no TX, valores altos podem
atrapalhar; reduza com `--threads 2` ou `4`.

As conexões extras são abertas pelo TX, sempre de volta ao **mesmo endereço** que ele já usa, e se autenticam
com um segredo aleatório de sessão enviado pelo canal já autenticado. Só aceitam leitura (`fetch`, `ping`,
`bench`); `scan`, `info` e as demais operações continuam restritas à conexão principal. Se uma conexão cair, os
arquivos dela passam para as outras; se todas caírem, a passada aborta e a retomada continua de onde parou.

### Diagnóstico de rede

```powershell
python -m mpm rx --netcheck            # RX
python -m mpm tx                       # TX
```

Mostra a latência, a vazão com 1 conexão e com N, e uma leitura do resultado. Não lê nem grava arquivos.
Serve para separar "a rede é o limite" de "o limite é o disco/antivírus/MPM". Um trecho de cabo ou porta
de 100 Mbit/s aparece como um teto de ~11 MB/s.

**Se o Firewall do Windows perguntar no RX, permita o acesso em redes privadas.**

### Destino

```text
<dest>/files/<pasta>/<caminho>     arquivos migrados (desktop, documents, pictures)
<dest>/mpm/manifest.json           manifesto do TX (última passada)
<dest>/mpm/inventory.jsonl         inventário arquivo a arquivo
<dest>/mpm/verified.jsonl          prova: pasta, caminho, tamanho e SHA-256 de cada cópia
<dest>/mpm/transfer-report.json    resumo das passadas, falhas e arquivos que mudaram
```

### Criar o usuário na máquina nova (0.3)

Com `--as-user`, o RX cria uma conta **local** do Windows e copia direto para as pastas reais do perfil
dela (`C:\Users\NOME\Desktop`, `Documents`, `Pictures`), em vez de uma pasta `files/`.

```text
python -m mpm account-check joao                  # SOMENTE LEITURA: valida o Windows, não cria nada
python -m mpm rx --as-user --dry-run              # mostra o que seria criado e copiado
python -m mpm rx --as-user                        # mesmo nome do perfil de origem
python -m mpm rx --as-user maria --admin          # outro nome; conta de administrador
python -m mpm rx --as-user --merge                # aceita um usuário que já existia (pode substituir arquivos)
```

- Exige terminal **como Administrador** no RX (o `--dry-run` não exige).
- A **senha não é migrada**: o RX pede uma nova (digitada duas vezes, oculta) depois da sua confirmação.
  Para automação use a variável de ambiente `MPM_NEW_USER_PASSWORD` (nunca há senha na linha de comando).
- Nada é criado antes de você confirmar. Se a cópia for interrompida, rode de novo: o MPM reconhece o
  usuário que ele mesmo criou (SID em `<dest>/mpm/target.json`) e continua. Um usuário que já existia antes
  só é usado com `--merge`.
- Sem `--dest`, os metadados (inventário, `verified.jsonl`, relatório) ficam em
  `%ProgramData%\MPM\migracao`; fora do perfil do usuário.
- Ao final, o RX tenta passar a propriedade dos arquivos para o usuário (`icacls /setowner`); as permissões
  já vêm herdadas da pasta do perfil. Falha aqui vira aviso, não erro.
- Contas Microsoft/Entra ID não são criadas por aqui; use conta local e vincule depois.
- As pastas são as padrão (`Desktop`, `Documents`, `Pictures`) do perfil novo; redirecionamentos
  (OneDrive "Known Folder Move") só passam a existir após o primeiro logon do usuário.
- Para desfazer um teste: `net user NOME /delete` (administrador) e apague `C:\Users\NOME`.

Implementação: `NetUserAdd`/`NetLocalGroupAddMembers` (netapi32) e `CreateProfile` (userenv) via `ctypes`; os
nomes dos grupos "Usuários"/"Administradores" são resolvidos pelo SID (o Windows é localizado).

### Inventário de aplicativos (0.4.0)

`python -m mpm rx --apps` pareia com o TX e **só gera o relatório**: nada é copiado nem instalado, e o TX só lê.

```text
python -m mpm rx --apps                       # resumo na tela; arquivos em %ProgramData%\MPM\migracao\mpm
python -m mpm rx --apps --dest C:\Migracao    # relatório em C:\Migracao\mpm
python -m mpm rx --apps --all                 # lista também sistema e drivers
python -m mpm rx --apps --ignore-app "^XAMPP" # trata como ruído o que casar (repetível)
```

O TX lê o registro (`Uninstall`, 64 e 32 bits e o usuário que executa o TX) e roda `winget export`. O RX separa:

| Grupo | O que é | Destino |
|---|---|---|
| winget | tem ID de pacote | entra no `winget-import.json` |
| sem pacote | programa de verdade, o winget não o conhece | instalar à mão (0.4.1 tenta achar pacote) |
| manuais | Office, antivírus gerenciado, plugin bancário | com nota do que fazer |
| drivers/OEM | Dell, Intel, Realtek, Qualcomm... | ignorados: o hardware é outro |
| sistema | VC++, .NET, WindowsAppRuntime, Edge... | ignorados: vêm com os programas |

Arquivos: `apps-report.txt` (completo), `apps-inventory.json` (dados brutos e classificação) e
`winget-import.json` (só os instaláveis, no formato do `winget import`).

Limitações: programas instalados só para *outros* usuários do TX não aparecem (só os da máquina e os do usuário que
roda o TX); programas da Microsoft Store entram só se o winget os reconhecer; a classificação de ruído e de drivers
é por padrões de nome (use `--ignore-app` para ajustar).

### Instalação dos programas (0.4.1)

`--install` (junto de `--apps`) instala **nesta máquina (RX)** os programas do relatório, um a um, com
`winget install --id ID --exact --silent`, e grava `install-report.json`. Há dois modos:

```text
python -m mpm rx --apps --install                  # pergunta: 1) Express  2) Custom  3) não instalar
python -m mpm rx --apps --install express          # tudo o que está marcado, sem perguntar nada
python -m mpm rx --apps --install custom           # lista numerada: você marca/desmarca cada item
python -m mpm rx --apps --install express --dry-run  # só mostra o que faria
python -m mpm rx --apps --install -y               # igual a express (-y não combina com custom)
python -m mpm rx --apps --install express --pin-versions   # mesma versão do TX (padrão: a mais recente)
```

**Express** instala os pacotes do winget marcados e as *sugestões* que o winget confirmar. **Custom** abre um menu
no próprio console: **↑ ↓** movem, **ESPAÇO** marca/desmarca, **PgUp/PgDn/Home/End** pulam, `a` = todos, `n` = nenhum,
`p` = voltar ao padrão, **ENTER** = instalar, `q`/Esc = cancelar. A lista rola dentro da janela, qualquer que seja o
tamanho dela. (Sem console interativo, cai numa lista numerada em que se digita `3,5-8` para alternar.)

`--only REGEX` marca só os itens cujo ID ou nome casar (ex.: `--install custom --only notepad` abre o menu com só o
Notepad++ marcado; `--install express --only "7zip|vlc"` instala só esses dois, sem perguntar).

Desmarcados por padrão (só entram se você marcar no Custom): driver de token SafeSign e Bonjour. *Sugestões* são
programas sem pacote no registro do winget, mas com ID provável conhecido (Chrome, Firefox, Brave, WinSCP, Acrobat
Reader, XAMPP, Python, RealVNC Viewer, Ghostscript, Packet Tracer): o RX confere que o ID existe (`winget show`) e,
se não existir, marca como "indisponível". O restante continua manual. Uma falha não interrompe os demais itens.
Alguns instaladores pedem permissão de administrador; rode o PowerShell como administrador para evitar pausas.

Antes de instalar, o RX confere o que **ele mesmo já tem** (registro e `winget export` locais). O que já está aqui
(mesmo ID do winget ou mesmo programa no registro) aparece com `✓ já instalado`, com a versão local na nota, e fica
**desmarcado**: o Express pula e o Custom só instala se você marcar. Se essa verificação falhar, vira só um aviso
e tudo segue como antes (o `winget` ainda responde "já instalado" e o item é registrado como `ja_instalado`).

### Dados e configurações dos programas (0.5.0 — só inventário)

`python -m mpm rx --appdata` pareia com o TX e **só mede**: nada é copiado. Para cada programa do catálogo ele diz
quanto existe de dados (sem cache), em quantos arquivos, e se há chaves de registro; grava `appdata-report.txt` e
`appdata-inventory.json` em `%ProgramData%\MPM\migracao\mpm`. Pode vir junto de `--apps`.

Catálogo inicial: Chrome, Edge, Brave, Firefox, PuTTY (sessões), WinSCP, Notepad++, 7-Zip, FileZilla, VLC, OBS,
VS Code, Cura, PrusaSlicer, Arduino IDE, draw.io, DB Browser, OpenVPN (perfis), Outlook (assinaturas e .pst), chaves
SSH, Git e unidades de rede mapeadas. Pastas de cache (Chrome `Cache`/`Code Cache`/`Service Worker`...) não entram na
conta. Lê o **perfil do TX que você escolher** (`--source-user NOME`, ou ele pergunta, como na cópia do perfil): rode
o TX com um usuário administrador *diferente* do migrado e nada fica em uso. Limitações: **senhas e cookies do Chrome/Edge/Brave não migram** (cifrados pela
conta do Windows, DPAPI: entre na conta do navegador no RX). A cópia
(0.5.1) usará o mesmo catálogo, com menu Express/Custom como na instalação de programas.

### Copiar as configurações dos programas (0.5.1)

Depois de instalar os programas no RX (`--apps --install`), `--settings` leva as **configurações** deles do TX para um
usuário do RX, com o mesmo motor verificado da migração do perfil (várias conexões, retomada, SHA-256 por arquivo,
atributos). Os arquivos vão para as pastas certas do usuário de destino e as chaves do registro (PuTTY, WinSCP, 7-Zip,
unidades de rede) são importadas no hive dele.

```text
python -m mpm rx --settings --to-user maria                 # pergunta: 1) Express  2) Custom  3) não copiar
python -m mpm rx --settings express --to-user maria         # tudo o que o TX tem, sem perguntar
python -m mpm rx --settings custom --to-user maria          # menu: setas/ESPAÇO para marcar cada programa
python -m mpm rx --settings express --only "chrome|putty" --to-user maria
python -m mpm rx --settings express --dry-run --to-user maria   # só mostra o que faria
python -m mpm rx --settings express                         # sem --to-user: o usuário atual do RX
```

- `--to-user NOME` exige que o usuário exista **e já tenha perfil** (use antes `rx --as-user NOME`, ou entre nele uma
  vez) e o PowerShell como **administrador** (grava no perfil dele e carrega o `NTUSER.DAT`). O usuário não pode estar
  com sessão aberta (o hive estaria em uso): o relatório avisa. Sem `--to-user`, vai para o usuário atual; feche antes
  os programas no RX, para não haver arquivos em uso.
- **De qual usuário do TX?** `--source-user NOME` (ou ele pergunta, como na cópia do perfil). O TX lê direto a pasta e o
  registro (`NTUSER.DAT`) desse perfil, então o ideal é rodar o TX como um administrador *diferente* do usuário migrado:
  sem sessão dele aberta, nenhum arquivo está em uso (se o usuário estiver logado, o MPM lê o registro dele já carregado).
  O TX precisa estar como **administrador** para ler outro perfil. Se for o mesmo usuário que roda o TX, vale o ambiente atual.
- Os arquivos copiados passam a pertencer ao usuário de destino (`icacls /setowner`, melhor esforço).
- Metadados em `%ProgramData%\MPM\migracao\mpm\settings` (`inventory.jsonl`, `verified.jsonl`,
  `transfer-report.json`, `settings-report.json` e os `.reg` exportados em `registry\`). Os `.reg` podem conter dados
  de sessões salvas: trate a pasta como sensível e apague depois.
- O TX só exporta chaves de registro do catálogo e só lê pastas do catálogo; nada fora dele é aceito.

### Wi-Fi salvo (0.5.2)

O mesmo `--settings` também leva, no mesmo menu Express/Custom, os **Wi-Fi salvos** do TX. (Impressoras ficaram fora
da primeira versão: dependem do driver do fabricante.)

- O TX exporta os perfis com `netsh wlan export profile key=clear` (**abra o TX como administrador**, senão as senhas
  não vêm e o perfil sem senha aparece desmarcado) e o RX importa com `netsh wlan add profile user=all` (vale para a
  máquina inteira, não só para o `--to-user`). A senha viaja só pelo canal TLS do pareamento e **nunca** vai para
  relatório, log ou arquivo gravado. Perfis corporativos (802.1X) vão, mas podem pedir usuário/certificado ao conectar.
  Perfil já salvo com o mesmo nome é mantido.
- O RX precisa do PowerShell como **administrador** (`--settings` avisa; só `--dry-run` dispensa). Para escolher só
  o Wi-Fi: `--only "^wifi:"` (ou `--only wifi:NomeDaRede`).

### Tudo de uma vez (0.5.6)

```text
python -m mpm rx --as-user maria --admin --full            # pergunta Express/Custom uma vez
python -m mpm rx --as-user maria --full express -y         # sem perguntas
python -m mpm rx --as-user maria --full --source-user Munhos
```

Numa só conexão com o TX: escolhe o perfil de origem **uma vez**, depois (1) cria o usuário e copia o perfil,
(2) instala os programas, (3) copia configurações e Wi-Fi para o usuário novo. Se a cópia do perfil terminar com
pendências, as etapas 2 e 3 não rodam (rode de novo para retomar). Exige `--as-user NOME`; não combina com
`--settings/--apps/--appdata/--install/--only/--to-user`. Código de saída: 0 ok, 1 perfil com pendências,
4 erro, 5 programa/configuração com falha.

### Log da execução (0.5.9)

`rx` e `tx` gravam tudo o que aparece na tela, com data e hora, em um arquivo por execução:

- Pasta: `C:\ProgramData\MPM\logs` (no Windows; em outro sistema `~/.mpm/logs`). `MPM_LOG_DIR` troca a pasta.
- Nome: `mpm-rx-AAAAMMDD-HHMMSS-<pid>.log` / `mpm-tx-...`. Guarda os 30 mais recentes.
- Tem versão, argumentos, mensagens, erros (com traceback se o programa quebrar) e o código de saída.
- **Não grava segredos:** código de pareamento, `--code`, senhas e chaves Wi-Fi viram `***`; o que você digita
  (senha do usuário novo, código no TX) nunca passa pela saída.
- `--no-log` desliga. O caminho do arquivo aparece no início e no fim da execução.
- Os relatórios da migração (`transfer-report.json`, `verified.jsonl`...) continuam em `<destino>\mpm\`.

### Ajustes da 0.5.10 (primeiro teste completo `--full`)

- **Administrador antes de tudo:** `rx --as-user/--settings/--full` confere isso *antes* de escutar e parear.
- **Caminhos longos (> 260 caracteres):** o RX (e o TX ao ler) usa o prefixo `\\?\\`; antes 3 arquivos de
  extensões do Firefox falhavam com "No such file".
- **Arquivos em uso no TX** (Chrome aberto: `LOCK`, `Cookies`, `Sessions`...): em `--settings` viram **aviso**
  ("em uso na origem"), não falha, e o código de saída fica 0. Para levá-los, feche o navegador no TX e rode de
  novo. Na cópia do perfil continuam sendo pendência.
- **Programas:** ID sugerido que não existe no winget não conta como falha; o fim da instalação lista
  "INSTALE À MÃO" com a explicação (hash que não confere, instalador removido/404...).
- **Log:** a barra de progresso sai marcada `[PROG]`, não `[ERR]`.

### mpm.exe (0.5.5): sem instalar Python

Para o PC antigo (TX) e o novo (RX) não precisarem de Python, o MPM vira uma **pasta portátil** com o `mpm.exe`
(PyInstaller, modo `onedir`: bem menos alertas de antivírus do que um `.exe` único). O `.exe` é gerado **no Windows**:

```text
# no Windows com Python 3.12+ instalado, na pasta do projeto
powershell -ExecutionPolicy Bypass -File packaging\build_exe.ps1
# gera dist\mpm-<versão>-win64.zip  (descompacte onde quiser, ex.: pendrive)
```

- **Mesmos comandos:** `mpm.exe tx`, `mpm.exe rx --as-user NOME --admin`, `mpm.exe rx --apps --install`,
  `mpm.exe rx --settings --to-user NOME` (é só trocar `python -m mpm` por `mpm.exe`).
- **Duplo clique** (sem argumentos) abre um menu com só duas opções: 1) PC ANTIGO (TX) e 2) PC NOVO (RX). Dentro do
  NOVO: tudo de uma vez, só o perfil, só os programas, só configurações e Wi-Fi. Ele só monta o comando e o roda;
  avisa se não estiver como administrador.
- **Administrador:** como antes, abra com botão direito → *Executar como administrador* (ou use o PowerShell como
  administrador). O `.exe` não força o UAC de propósito: assim a saída continua na mesma janela.
- **Antivírus:** o Windows Defender pode reclamar de um programa novo sem assinatura. O pacote `onedir` e sem UPX
  reduz isso; se ele bloquear, adicione a pasta às exclusões. Assinar o executável é um passo futuro.
- Se o build falhar com o Python 3.14, use o 3.13 (`py -3.13 -m venv`), ou me mande a mensagem de erro.

### Descoberta automática

- O RX responde a sondagens UDP **somente enquanto espera o TX**; ao parear, deixa de aparecer.
- A resposta leva só versão do MPM, nome do computador e porta TCP. **Nada derivado do código de pareamento**
  circula em claro: o código só é verificado dentro do canal TLS.
- A sondagem tem tamanho mínimo e o RX ignora as menores, de modo que a resposta nunca é maior que o pedido
  (sem amplificação por IP forjado); o RX também limita o ritmo de respostas.
- Funciona na **mesma sub-rede**. Não atravessa roteadores/VLANs e falha em Wi-Fi com "isolamento de clientes";
  nesses casos use `--rx`.

### Segurança

- Canal TLS 1.3 com certificado efêmero gerado a cada sessão no RX.
- O certificado não é validado por CA. A autenticidade vem do **código de pareamento**: os dois lados provam
  que o conhecem por HMAC que inclui a impressão digital do certificado que cada um viu, então um
  intermediário não consegue se passar por nenhum dos dois.
- 3 tentativas de pareamento falhas encerram a sessão.
- O TX só serve arquivos dentro das 3 pastas do perfil escolhido; o RX só grava dentro do destino.
  Caminhos com `..`, absolutos, links ou pastas fora do escopo são recusados nos dois lados (o RX valida cada componente
  sem consultar `realpath`, para não falhar durante a criação concorrente de pastas).
- Use somente em rede confiável (LAN). Não exponha a porta à internet.

### Limitações conhecidas

- Atributos **Somente leitura, Oculto e Sistema** são preservados (0.3.1), por exemplo o `desktop.ini`. Arquivos
  copiados antes da 0.3.1 e ainda iguais na origem são pulados e não ganham os atributos: apague o destino para refazer.
- Pastas vazias, datas de criação, permissões (ACLs), proprietário e streams alternativos do NTFS não são
  preservados. Links e junctions são ignorados.
- Arquivos só-na-nuvem (OneDrive) serão baixados ao serem lidos pelo TX.
- A descoberta automática supõe sub-rede /24 para o broadcast dirigido e usa também 255.255.255.255.
- Em cada conexão os arquivos vão um por vez; o ganho vem das conexões em paralelo.
- 0.3: o código de contas só foi testado com um backend simulado; as chamadas ao Windows precisam de validação em campo (comece por `account-check`).

### MPM pessoal: perfil inteiro + Outlook (`--meu`, 0.5.12)

```powershell
mpm.exe rx --as-user NOME [--admin] [--merge] --meu
```

Para mover **o meu usuário** de um PC para outro: copia o perfil inteiro (Área de Trabalho, Documentos, Downloads,
Imagens...) e, em seguida, só o **Outlook clássico**. Não instala programas, não copia Wi-Fi nem outras
configurações, não pergunta nada e não pausa entre as passadas. No menu: PC NOVO → opção 5.

O que acompanha do Outlook: o perfil do registro (contas, servidores, portas, opções, painel de navegação) das
versões 2010–2021/365, assinaturas, modelos (`NormalEmail.dotm`, `.oft`), papéis de carta, dicionário pessoal,
autocompletar (RoamCache) e os arquivos `.pst` que estão no perfil.

- **Senhas das contas não vão** (ficam cifradas com a conta do Windows e não abrem em outro usuário/PC): o Outlook
  pede a senha de cada conta uma vez; contas Microsoft 365/Exchange pedem o login de novo. Todo o resto vem pronto.
- Feche o Outlook nos dois PCs e **não abra o Outlook no PC novo antes** da migração (ele criaria um perfil novo).
- `.pst` **fora** do perfil (ex.: `D:\Email`) não são copiados: o relatório lista cada um e avisa. Se o usuário novo
  tem nome diferente do antigo, o Outlook pede para localizar os `.pst` (use *Procurar* na pasta nova).
- `.ost` (cache do servidor) não é copiado; o Outlook baixa de novo.
- Novo Outlook (aplicativo `olk.exe`) guarda tudo na nuvem: não há o que migrar.

## Discovery (sem rede)

```powershell
python -m mpm                                    # sistema + usuário
python -m mpm profiles                           # perfis da máquina (* = atual)
python -m mpm scan [--user NOME] [--no-appdata]  # varre pastas do perfil e resume o AppData
python -m mpm manifest -o output\manifest.json   # manifesto + inventário de arquivos
python -m mpm validate output\manifest.json
```

## Testes

```bash
python3 -m unittest discover -s tests -t . -v
```

## Estrutura

```text
mpm/
├── mpm/
│   ├── core/            # lógica neutra de plataforma
│   │   ├── models.py, system.py, user.py, profiles.py
│   │   ├── filesystem.py   # varredura, inventário JSONL, resumo do AppData
│   │   ├── manifest.py     # manifesto (contrato TX <-> RX)
│   │   ├── accounts.py     # validação de nomes e contrato dos backends de conta
│   │   ├── target.py       # destino = perfil real (plan/commit/finalize)
│   │   └── fmt.py
│   ├── net/             # transporte
│   │   ├── discovery.py    # localiza o RX na LAN (broadcast UDP)
│   │   ├── netcheck.py     # latência e vazão (1 x N conexões)
│   │   ├── security.py     # código de pareamento, TLS efêmero, HMAC
│   │   ├── handshake.py    # autenticação mútua
│   │   ├── protocol.py     # enquadramento de mensagens
│   │   ├── safepath.py     # caminhos seguros (nos dois lados)
│   │   ├── tx.py           # lado antigo: somente leitura
│   │   ├── rx.py           # lado novo: listener e cliente de pedidos
│   │   └── migration.py    # plano, cópia verificada, passadas, relatório
│   ├── platforms/       # tudo que é específico de SO
│   │   ├── linux/
│   │   └── windows/        # stdlib: ctypes, winreg, whoami; accounts.py = NetUserAdd/CreateProfile
│   └── cli.py
└── tests/
```

## Regras

- O `core/` nunca importa `platforms.linux` ou `platforms.windows` diretamente; usa `platforms.current()`.
- Links simbólicos e junctions nunca são seguidos.
- Nada recebido pela rede é confiável: todo caminho passa por `safepath.resolve_in_root`.

## Roadmap

- [x] 0.1 Discovery (sistema, usuário, perfis, pastas, AppData em resumo, manifesto)
- [x] 0.2 Rede: RX/TX, pareamento, cópia verificada, duas passadas, retomada por arquivo
- [x] 0.2.1 TX localiza o RX sozinho (broadcast UDP); lista de RX por nome quando há mais de um
- [x] 0.2.2 Cópia em paralelo (--threads), retomada no meio do arquivo, tentativas com espera, `--netcheck`
- [x] 0.3 RX cria o usuário e o perfil (`--as-user`, CreateProfile), propriedade dos arquivos; destino = perfil real (a validar em campo)
- [x] 0.3.1 Preserva atributos Somente leitura/Oculto/Sistema (a validar em campo)
- [x] 0.3.2 Criação de pastas serializada e refeita em caso de erro transitório (190 falhas na passada 1 com 8 conexões)
- [x] 0.3.3 RX valida caminhos sem `realpath` (253 falsas falhas "fora da pasta permitida" na passada 1 com 8 conexões)
- [x] 0.4.0 Inventário de aplicativos do TX (`rx --apps`): relatório e `winget-import.json` (a validar em campo)
- [x] 0.4.1 Instalação no RX (`rx --apps --install`, `winget install` item a item com confirmação) e sugestão de ID para os sem pacote (a validar em campo)
- [x] 0.4.2 Instalação: menu com setas/espaço (Custom), Express, `--only`, e o RX pula o que já tem instalado (a validar em campo)
- [x] 0.5.0 Inventário de dados e configurações por programa (`rx --appdata`, catálogo de plugins; a validar em campo)
- [x] 0.5.1 Cópia das configurações dos programas (`rx --settings`, arquivos + registro, Express/Custom, `--to-user`; a validar em campo)
- [x] 0.5.2 Wi-Fi salvo no `--settings` (validado em campo; impressoras foram retiradas na 0.5.4)
- [x] 0.5.3 `--as-user` combinado com `--settings/--apps/--appdata` agora é erro (antes era ignorado e gravava no usuário atual)
- [x] 0.5.4 `--settings`/`--appdata` leem o perfil do TX escolhido (`--source-user` ou pergunta), pasta e registro (hive) dele; impressoras saem do projeto (a validar em campo)
- [x] 0.5.5 Empacotamento: `mpm.exe` portátil (PyInstaller onedir), menu de duplo clique, `packaging\build_exe.ps1` (a validar no Windows)
- [x] 0.5.6 `rx --as-user NOME --full`: perfil + programas + configurações e Wi-Fi de uma vez; menu do `.exe` só ANTIGO/NOVO (a validar em campo)
- [x] 0.5.7 Menu do .exe mais claro: ENTER = recomendado, validação do nome do usuário, mostra o comando executado
- [x] 0.5.8 Menu do .exe: tela limpa entre menus e submenu compacto
- [x] 0.5.9 Log de cada execução do rx/tx (com data e hora, sem segredos) em %ProgramData%\MPM\logs
- [x] 0.5.10 Admin checado antes do pareamento; caminhos longos; arquivos em uso no TX viram aviso nas configurações; lista "instale à mão"
- [x] 0.5.11 Caminho longo também nas configurações (SettingsMigration tinha o próprio _target)
- [x] 0.5.12 `--meu`: perfil + Outlook (perfil de contas no registro, modelos, .pst) — a validar em campo
- [ ] 0.6 USB/offline como transporte alternativo
