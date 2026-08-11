# ps2-save-tools

Ferramentas para converter saves de PS2 entre os formatos que os sites de save
distribuem e os que o PCSX2 realmente consegue importar.

## cbs2psu.py

Converte um save **CodeBreaker (`.cbs`)** em **`.psu`**, que é o formato que o
editor de memory card do PCSX2 importa direto.

```bash
python3 cbs2psu.py entrada.cbs [saida.psu]
```

Sem dependências — só a stdlib do Python 3.

Exemplo:

```
$ python3 cbs2psu.py need-for-speed-most-wanted-2005.15263.cbs
need-for-speed-most-wanted-2005.15263.cbs
  directory : BASLUS-21351-Melz
  title     : NFS MW Melz
  files     : 3
      nfwMW_New.ico               31044 bytes
      BASLUS-21351-Melz           62689 bytes
      icon.sys                      964 bytes
  -> need-for-speed-most-wanted-2005.15263.psu  (99328 bytes)
```

### Como importar no PCSX2

Se a sua versão tiver o Memory Card Editor: Settings → Memory Cards → Edit →
Import. Se não tiver (é o caso de várias builds), use o `psu2mc.py` abaixo.

## psu2mc.py

Insere um `.psu` **dentro** de um memory card `.ps2` já existente, ao lado dos
saves que já estão lá. É o que fazer quando o PCSX2 não oferece Import.

```bash
# faça backup primeiro -- ele escreve no cartão in place
cp Mcd001.ps2 Mcd001-backup.ps2

python3 psu2mc.py Mcd001.ps2 save.psu
```

```
card: Mcd001.ps2
  8135 clusters, rootdir at 0
  existing: ['BASLUS-20622File', 'BASLUS-21351-JP']
  free clusters: 7673
  writing 'BASLUS-21351-Melz' (3 files)
      nfwMW_New.ico               31044 bytes -> cluster 465
      BASLUS-21351-Melz           62689 bytes -> cluster 496
      icon.sys                      964 bytes -> cluster 558
  done -- root now has 5 entries
```

Saves com o mesmo nome de pasta são recusados em vez de sobrescritos. Dois
saves do mesmo jogo convivem numa boa desde que as pastas tenham nomes
diferentes (`BASLUS-21351-JP` e `BASLUS-21351-Melz`, por exemplo) — o jogo
mostra os dois como slots separados.

### Formato do memory card

Um cartão de 8 MB tem 16384 páginas de 528 bytes: 512 de dados + 16 de spare.
Os primeiros 12 bytes do spare são quatro códigos de Hamming de 3 bytes, um
para cada bloco de 128 bytes da página. Um cluster são 2 páginas (1024 bytes).

Duas armadilhas que valem registro:

- Clusters de **dados** são contados a partir de `alloc_offset`; clusters de
  **FAT** e de FAT indireta são contados a partir do início do cartão. Misturar
  os dois faz você ler lixo no lugar da FAT.
- Os clusters de um diretório **não são necessariamente contíguos** — sempre
  siga a cadeia da FAT em vez de assumir páginas sequenciais.

Páginas nunca usadas ficam com o ECC zerado no lugar do valor calculado, então
um cartão saudável "falha" numa verificação ingênua de ECC na maioria das
páginas. Isso é normal, não é corrupção.

### O Game ID precisa bater

O PS2 casa o save com o jogo pela pasta `BA<GAMEID>`. Se o `directory` do save
for `BASLUS-21351` e sua ISO bootar outro ID, o jogo simplesmente não enxerga o
save — ele fica no cartão, invisível.

Para descobrir o ID real da sua ISO (o nome do arquivo mente com frequência —
rips etiquetados "Black Edition" que na verdade são a versão comum são comuns):

```bash
strings -n 8 "sua-iso.iso" | grep -m1 BOOT2
# BOOT2 = cdrom0:\SLUS_213.51;1   ->  SLUS-21351
```

IDs do Most Wanted (2005):

| Versão              | Game ID     |
| ------------------- | ----------- |
| Most Wanted US      | SLUS-21351  |
| Black Edition US    | SLUS-21575  |
| Most Wanted EU      | SLES-53658  |

## Formato CBS

```
0x00  'CFU\0'
0x04  u32  desconhecido
0x08  u32  tamanho do header (o payload começa aqui)
0x0C  u32  tamanho descomprimido
0x10  u32  tamanho comprimido
0x14  char[32]  nome do diretório (ex.: BASLUS-21351-Melz)
0x34  timestamps, mode, e o título de exibição
hlen  payload: RC4 e depois zlib
```

O RC4 do CodeBreaker é fora do padrão: em vez de derivar a permutação a partir
de uma chave, ele começa de uma **S-box fixa** — essa tabela *é* a chave, e não
existe key schedule. Descriptografado, o payload é um zlib stream; dentro dele
vêm entradas de 64 bytes, cada uma seguida pelos dados do arquivo.

O `.psu` é bem mais simples: entradas de 512 bytes para o diretório, `.` e `..`,
depois uma por arquivo, com os dados de cada arquivo completados com zeros até
um múltiplo de 1024.

## Créditos

O formato CBS (a S-box do RC4 e o layout das entradas) vem da implementação de
referência do [ps2dev/mymc](https://github.com/ps2dev/mymc), em `ps2save.py`.
