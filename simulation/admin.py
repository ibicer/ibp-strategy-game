from django.contrib import admin
from .models import *

admin.site.register([Game, GameMembership, Product, ScenarioYear, DemandValue, ForecastSnapshot, AnnualPlan, MonthlyDecision, ProductDecision, PlayerYearScore])
