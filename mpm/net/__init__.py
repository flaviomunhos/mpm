"""Transporte de rede do MPM.

Papéis:
  RX  máquina nova. Escuta, controla a migração e escreve os arquivos.
  TX  máquina antiga. Conecta ao RX e apenas responde (somente leitura).
"""
