import pandas

df = pandas.read_csv('para-data/19sep/observations.csv')
pandas.set_option('display.max_columns',None)
print(df.head(3))