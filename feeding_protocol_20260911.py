"""Protocolo de 11/09/2026: pesos/taxas transcritos do PDF do proprietário.

Sobrevivência: dia 1 = 100%; sem saltos de transferência nos dias 26/46.
Os totais são recalculados (população x peso x taxa), pois a curva foi corrigida.
As misturas seguem as transições 75/25, 50/50 e 25/75 do PDF.
"""
REVISION = '2026-09-11-daily-survival-v1'
SOURCE_ROWS = [(1, 'PL10', 0.0025, 35.0),
 (2, 'PL11', 0.003, 30.0),
 (3, 'PL12', 0.005, 28.0),
 (4, 'PL13', 0.006, 26.88),
 (5, 'PL14', 0.007, 25.8),
 (6, 'PL15', 0.008, 24.77),
 (7, 'PL16', 0.01, 23.78),
 (8, 'PL17', 0.011, 22.83),
 (9, 'PL18', 0.013, 21.92),
 (10, 'PL19', 0.016, 21.04),
 (11, 'PL20', 0.019, 20.2),
 (12, 'PL21', 0.022, 19.39),
 (13, 'PL22', 0.026, 18.62),
 (14, 'PL23', 0.03, 17.87),
 (15, 'PL24', 0.036, 17.16),
 (16, 'PL25', 0.042, 16.47),
 (17, 'PL26', 0.05, 15.81),
 (18, 'PL27', 0.059, 15.02),
 (19, 'PL28', 0.069, 14.27),
 (20, 'PL29', 0.081, 13.56),
 (21, 'PL30', 0.096, 12.88),
 (22, 'PL31', 0.113, 12.23),
 (23, 'PL32', 0.133, 11.62),
 (24, 'PL33', 0.157, 11.04),
 (25, 'PL34', 0.185, 10.49),
 (26, 'J35', 0.218, 9.96),
 (27, 'J36', 0.257, 9.47),
 (28, 'J37', 0.303, 8.99),
 (29, 'J38', 0.356, 8.54),
 (30, 'J39', 0.42, 8.12),
 (31, 'J40', 0.49, 7.71),
 (32, 'J41', 0.535, 7.33),
 (33, 'J42', 0.58, 6.96),
 (34, 'J43', 0.6177, 6.82),
 (35, 'J44', 0.7, 6.68),
 (36, 'J45', 0.87, 6.55),
 (37, 'J46', 1.05, 6.42),
 (38, 'J47', 1.6, 6.29),
 (39, 'J48', 1.82, 6.16),
 (40, 'J49', 2.05, 6.04),
 (41, 'J50', 2.3, 5.92),
 (42, 'J51', 2.55, 5.8),
 (43, 'J52', 2.8, 5.69),
 (44, 'J53', 3.05, 5.57),
 (45, 'J54', 3.35, 5.46),
 (46, 'A55', 3.65, 5.41),
 (47, 'A56', 3.95, 5.35),
 (48, 'A57', 4.25, 5.3),
 (49, 'A58', 4.55, 5.25),
 (50, 'A59', 4.85, 5.19),
 (51, 'A60', 5.15, 5.14),
 (52, 'A61', 5.45, 5.09),
 (53, 'A62', 5.75, 5.04),
 (54, 'A63', 6.05, 4.99),
 (55, 'A64', 6.35, 4.94),
 (56, 'A65', 6.65, 4.89),
 (57, 'A66', 6.95, 4.84),
 (58, 'A67', 7.15, 4.79),
 (59, 'A68', 7.35, 4.74),
 (60, 'A69', 7.5, 4.7),
 (61, 'A70', 7.79, 4.65),
 (62, 'A71', 8.07, 4.6),
 (63, 'A72', 8.35, 4.56),
 (64, 'A73', 8.63, 4.51),
 (65, 'A74', 8.92, 4.47),
 (66, 'A75', 9.2, 4.42),
 (67, 'A76', 9.49, 4.38),
 (68, 'A77', 9.77, 4.33),
 (69, 'A78', 10.05, 4.29),
 (70, 'A79', 10.35, 4.25),
 (71, 'A80', 10.65, 4.2),
 (72, 'A81', 11.11, 4.16),
 (73, 'A82', 11.57, 4.12),
 (74, 'A83', 12.04, 4.08),
 (75, 'A84', 12.5, 4.04)]

LABELS = ['NutriSphera 225', 'NutriSphera 450', 'AquaVita 40/1 (0,5-1,0mm)',
          'AquaVita 40/2 (1,0-1,8mm)', 'AquaVita JUV. 38 (1,5mm)', 'AquaVita 35 (2mm)']

def build_compact_rows(previous_rows=()):
    # Os controles de água são independentes desta atualização de alimentação.
    previous_water = {row[3]: row[11] for row in previous_rows}
    survival = 100.0
    rows = []
    for day, stage, weight, rate in SOURCE_ROWS:
        loss = (0 if day == 1 else 1.5 if day == 3 else
                0.5 if day in (2, 4, 5) or day >= 53 else 0.25)
        survival = round(survival - loss, 2)
        phase = 'bercario' if day <= 25 else 'juvenil' if day <= 45 else 'engorda'
        phase_day = day if day <= 25 else day - 25 if day <= 45 else day - 45
        population = round(350000 * survival / 100)
        biomass = population * weight / 1000
        total = round(population * weight * rate / 100)
        index = 0
        blend = None
        for start, old in [(12, 0), (17, 1), (28, 2), (38, 3), (55, 4)]:
            if day >= start + 3:
                index = old + 1
            elif day >= start:
                blend = (old, (day - start + 1) / 4)
                break
        if blend:
            old, share = blend
            new_g = round(total * share)
            mixes = [(LABELS[old], total-new_g), (LABELS[old+1], new_g)]
        else:
            mixes = [(LABELS[index], total)]
        rows.append((phase, phase_day, day, stage, population, survival, weight,
                     round(biomass, 2), rate, total, mixes, previous_water.get(stage, [])))
    return rows
