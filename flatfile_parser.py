#!/usr/bin/python

# This is a utility program designed to take DHS datasets, in flat file format,
# and read them into pandas dataframes.  It will then output said dataframes
# as SQL tables.
# DHS _does_ provide e.g. STATA files and SAS files, but the formatting on
# them is such that the standard pandas.read_stata and .read_sas do not work
# well. (And the one workaround I've found online doesn't.)

#import csv
import glob
import optparse
import os
#import pandas
import re
import shutil
import tempfile
import timeit
import zipfile

from odo import drop, odo
#from sqlalchemy import create_engine

# India has data broken down at the province / district level, with a two-
# letter code for each region.  This causes a problem when we hit Kerala,
# as KE is _also_ the country code for Kenya.  Solution: Rename the Kerala
# files as K2XXXXXX upon download.
COUNTRY_CODES = { "AF" : "Afghanistan", "BD" : "Bangladesh", "KH" : "Cambodia",
    "HT" : "Haiti", "AP" : "India", "AR" : "India", "AS" : "India",
    "BH" : "India", "DL" : "India", "GJ" : "India", "GO" : "India",
    "HP" : "India", "HR" : "India", "IA" : "India", "JM" : "India",
    "KA" : "India", "K2" : "India", "MG" : "India", "MH" : "India", 
    "MN" : "India", "MP" : "India", "MZ" : "India", "NA" : "India", 
    "OR" : "India", "PJ" : "India", "RJ" : "India", "SK" : "India",
    "TN" : "India", "TR" : "India", "UP" : "India", "WB" : "India",
    "ID" : "Indonesia", "KE" : "Kenya", "MV" : "Maldives", "MM" : "Myanmar",
    "NP" : "Nepal", "PK" : "Pakistan", "PH" : "Philippines",
    "LK" : "Sri Lanka", "TH" : "Thailand", "TL" : "Timor-Leste",
    "TZ" : "Tanzania", "UG" : "Uganda", "VN" : "Vietnam",
    "BR" : "Brazil", "CO" : "Colombia", "DR" : "Dominican Republic",
    "EC" : "Ecuador", "ES" : "El Salvador", "GU" : "Guatemala",
    "HN" : "Honduras", "MX" : "Mexico", "NC" : "Nicaragua" }

INDIA_STATE_CODES = { "AP" : "Andhra Pradesh", "AR" : "Arunachal Pradesh",
    "AS" : "Assam", "BH" : "Bihar", "DL" : "Delhi", "GJ" : "Gujarat",
    "GO" : "Goa", "HP" : "Himachal Pradesh", "HR" : "Haryana",
    "IA" : "All-India", "JM" : "Jammu and Kashmir", "KA" : "Karnataka",
    "K2" : "Kerala", "MG" : "Meghalaya", "MH" : "Maharashtra", 
    "MN" : "Manipur", "MP" : "Madhya Pradesh", "MZ" : "Mizoram",
    "NA" : "Nagaland", "OR" : "Odisha/Orissa", "PJ" : "Punjab",
    "RJ" : "Rajasthan", "SK" : "Sikkim", "TN" : "Tamil Nadu", "TR" : "Tripura",
    "UP" : "Uttar Pradesh", "WB" : "West Bengal" }

DATASET_CODES = { "AN" : "Antenatal", "AT" : "Antiretroviral",
    "CL" : "Check List", "CN" : "Consultations", "CO" : "Community",
    "CS" : "Country Specific", "CT" : "HIV Counseling",
    "FC" : "Facility Inventory", "FP" : "Family Planning",
    "IN" : "Safe Injection", "IP" : "Inpatient Unit", "LB" : "Laboratory",
    "LD" : "Labor Delivery", "MS" : "MHIS", "OI" : "Out/Inpatient",
    "OP" : "Outpatient Unit", "PH" : "Pharmacy", "PI" : "Personal Interview",
    "PM" : "PMTCT", "PV" : "Provider", "SC" : "Sick Child", 
    "SI" : "Sexually Transmitted Infections", "SL" : "Staff Listing",
    "TB" : "Tuberculosis", "IR" : "Women Recode", "BR" : "Birth Recode",
    "CR" : "Couple Recode", "HR" : "Household Recode", "MR" : "Male Recode",
    "KR" : "Children Recode", "PR" : "Household Member Recode", 
    "HH" : "Household Raw", "PQ": "Household Member Raw",
    "IQ" : "Individual Women Raw", "IH" : "Individual/Household Raw",
    "ML" : "Male Raw", "PG" : "Parent/Guardian Raw", "SM" : "Safe Motherhood", 
    "SQ" : "Service Availability Raw", "VR" : "Village Recode",
    "AR" : "AIDS Recode", "OB" : "Other Biomarkers", "HT" : "HIV Test Raw",
    "GE" : "Geographic" }

INDEX_COLUMNS = [ "Facility number", "Unit line number", "unit type",
    "provider line number", "Case Identification", "Cluster number",
    "Household number", "Respondent\'s line number", "Country code" ]

FLAGGED_CASES = 999999
BF_IDENTIFIER = "When child put to breast"
NULL_INT_VALUE = -999
NULL_FLOAT_VALUE = -999.0

RECORD_LIMIT = 0
MAX_COL_CNT = 700    # psycopg2, and hence Postgres, have a hard cap of 1600.
FLOAT_ERROR = 0.001


class DataDictionary:
  def __init__(self, name):
    self.name = name
    self.variable_dict = dict()         # Maps the variable label to vbl name
    self.variable_format_dict = dict()  # Maps variable label to vbl format
    self.vbls_seen = set()              # Maintains a list of vbl names seen.
    self.key_type = dict()              # Maps vbl label to encoded field type
    self.variable_type = dict()         # Maps vbl label to decoded field type
    self.bytewise_encoding = dict()     # Maps variable label to start+end pos.
    self.null_encoding = dict()    # Maps vbl label, null vals to display vals
    self.value_dict = dict()       # Maps vbl format, value, to display values
    
  def add_bytewise_encoding(self, start_pos, vbl_label, num_len_string):
    num_bytes = re.search("(\d+)\.", num_len_string).group(1)
    self.bytewise_encoding[vbl_label] = {
        "start_pos" : start_pos - 1,
        "end_pos"   : start_pos + int(num_bytes) - 1
        }
    decimal_pt = re.match("\d+\.(\d+)", num_len_string)
    if decimal_pt and int(decimal_pt.group(1)) > 0:
      self.key_type[vbl_label] = "float32"
    elif re.search("\$", num_len_string):
      self.key_type[vbl_label] = "string"
    else:
      self.key_type[vbl_label] = "int32"
    if vbl_label in self.variable_format_dict:
      self.variable_type[vbl_label] = "string"
    else:
      self.variable_type[vbl_label] = self.key_type[vbl_label]
            
  def add_null_rule(self, vbl_label, null_value):
    if not vbl_label in self.null_encoding:
      self.null_encoding[vbl_label] = dict()
    self.null_encoding[vbl_label][null_value] = NULL_INT_VALUE
    if vbl_label in self.variable_type:
      if self.variable_type[vbl_label] == "int32":
        null_value = int(null_value)
        self.null_encoding[vbl_label][null_value] = NULL_INT_VALUE
        return
      elif self.variable_type[vbl_label] == "float32":
        null_value = float(null_value)
        self.null_encoding[vbl_label][null_value] = NULL_FLOAT_VALUE
      else:
        if not vbl_label in self.null_encoding:
          self.null_encoding[vbl_label] = dict()
        self.null_encoding[vbl_label][null_value] = ""
        
  def clean_formats(self):
    erased_format_values = dict()
    formats_seen = set()
    for vbl_label in list(self.variable_format_dict.keys()):
      vbl_format = self.variable_format_dict[vbl_label]
      if vbl_format not in self.value_dict:
        del self.variable_format_dict[vbl_label]
        if vbl_format in erased_format_values:
          if not vbl_label in self.null_encoding:
            self.null_encoding[vbl_label] = dict()
          for value in erased_format_values[vbl_format]:
            null_value = erased_format_values[vbl_format][value]
            self.null_encoding[vbl_label][value] = null_value
        else:
          print("Missing value dictionary for format |" + vbl_format + "|")
        continue
      
      if (len(self.value_dict[vbl_format]) == 1 and
          vbl_format not in formats_seen):
        value = list(self.value_dict[vbl_format].keys())[0]
        print("Format |" + vbl_format + "| has one entry in its value " +
              "dictionary: |" + str(value) + " = " +
              str(self.value_dict[vbl_format][value]) + "|")
        # Maybe do something else with flagged cases?
        if self.value_dict[vbl_format][value] in ["Don't know", "DK", "DK ",
            "No calendar", "Flagged cases", "No births"]:
          if not vbl_label in self.null_encoding:
            self.null_encoding[vbl_label] = dict()
          self.add_null_rule(vbl_label, value)
          self.variable_type[vbl_label] = self.key_type[vbl_label]
          if self.value_dict[vbl_format][value] == "Flagged cases":
            self.null_encoding[vbl_label][value] = FLAGGED_CASES
            self.variable_type[vbl_label] = "int32"
          if self.value_dict[vbl_format][value] == "No births":
            self.null_encoding[vbl_label][value] = 0
            self.variable_type[vbl_label] = "int32"
          erased_format_values[vbl_format] = { value :
              self.null_encoding[vbl_label][value] }
          del self.value_dict[vbl_format]
      elif (len(self.value_dict[vbl_format]) == 2 and
            set(self.value_dict[vbl_format].values()) == set(["Yes", "No"])):
        for key in self.value_dict[vbl_format].keys():
          if self.value_dict[vbl_format][key] == "Yes":
            self.value_dict[vbl_format][key] = True
          else:
            self.value_dict[vbl_format][key] = False
        self.variable_type[vbl_label] = "bool"
        if vbl_label in self.null_encoding:
          del self.null_encoding[vbl_label]
            
      formats_seen.add(vbl_format)
      if vbl_format not in self.value_dict:            
        del self.variable_format_dict[vbl_label]
        
  def parse(self, record):
    record_dict = dict()
    for vbl_label in self.bytewise_encoding:
      bytedict = self.bytewise_encoding[vbl_label]
      if bytedict["end_pos"] >= len(record) - 1: continue  # Because of \r
      value = record[bytedict["start_pos"]:bytedict["end_pos"]]
      if vbl_label not in self.variable_dict:
        # Throw an exception
        print(vbl_label + ' not found in schema ' + self.name)
        continue
      vbl_name = self.variable_dict[vbl_label]
      if value == ' ' * len(value) or value == '*' * len(value):
        record_dict[vbl_name] = None
        if self.variable_type[vbl_label] == "string":
          record_dict[vbl_name] = ""
          continue
        elif self.variable_type[vbl_label] == "int32":
          record_dict[vbl_name] = NULL_INT_VALUE
        elif self.variable_type[vbl_label] == "float32":
          record_dict[vbl_name] = NULL_FLOAT_VALUE
        elif self.variable_type[vbl_label] == "bool":
          record_dict[vbl_name] = False
        continue
      # The data dictionary for first time of breastfeeding is special and
      # requires separate interpretation.
      if re.search(BF_IDENTIFIER, vbl_name):
        self.variable_type[vbl_label] = "string"
        value = int(value)
        if value == 0:
          record_dict[vbl_name] = "Immediately"
        elif value >= 100 and value < 200:
          record_dict[vbl_name] = str(value - 100) + " hours"
        elif value > 200 and value < 300:
          record_dict[vbl_name] = str(value - 200) + " days"
        else:
          # Throw an error.
          print(str(value) + " is not a valid value for " + vbl_name)
        continue
      if self.key_type[vbl_label] == "int32":
        try:
          value = int(value)
        except ValueError:
          print("Cannot parse |" + value + "| as int for field |" + vbl_label +
                "| in schema " + self.name)
          continue
      elif self.key_type[vbl_label] == "float32":
        try:
          value = float(value)
        except:
          print("Cannot parse |" + value + "| as float for field |" +
                vbl_label + "| in schema " + self.name)
          continue
        # Floats need some special handling, because rounding errors make
        # equality tricky.
        value_found = False
        if vbl_label in self.null_encoding:
          for null_value in self.null_encoding[vbl_label]:
            if abs(value - null_value) <= FLOAT_ERROR:
              record_dict[vbl_name] = self.null_encoding[vbl_label][null_value]
              value_found = True
              break
          if value_found: continue
        # Hard override: ignore all variable format dicts for floats.
        # Just use the encoded value.
        record_dict[vbl_name] = value
        continue
#        if vbl_label in self.variable_format_dict:
#          vbl_format = self.variable_format_dict[vbl_label]
#          if vbl_format in self.value_dict:
#            for mapped_value in self.value_dict[vbl_format]:
#              if abs(value - mapped_value) <= FLOAT_ERROR:
#                record_dict[vbl_name] = str(
#                    self.value_dict[vbl_format][mapped_value])
#                value_found = True
#                break
#            if value_found:
#              continue
#            else:
#              record_dict[vbl_name] = value
#              continue
#          else:
#            # Throw an exception
#            print(vbl_format + ' value dictionary not found.')
#            continue
#        else:
          
      if (vbl_label in self.null_encoding and 
          value in self.null_encoding[vbl_label]):
        record_dict[vbl_name] = self.null_encoding[vbl_label][value]
      elif vbl_label in self.variable_format_dict:
        vbl_format = self.variable_format_dict[vbl_label]
        if vbl_format in self.value_dict:
          if value in self.value_dict[vbl_format]:
            record_dict[vbl_name] = self.value_dict[vbl_format][value]
          # We sometimes have multiple-choice answers, coded by characters.
          elif self.key_type[vbl_label] == "string":
            display_vals = []
            #print("Vbl label = |" + vbl_label + "|")
            for encoded_val in self.value_dict[vbl_format]:
              display_value = self.value_dict[vbl_format][encoded_val]
              #print("Encoded = |" + str(encoded_val) + "|" + str(display_value) + "|")
              if re.search(encoded_val, value):
                display_vals.append(display_value)
            record_dict[vbl_name] = ', '.join(display_vals)
          else:
            # Record the value anyway.
            record_dict[vbl_name] = str(value)
            if (vbl_label in self.variable_type and
                self.variable_type[vbl_label] == "bool"):
              record_dict[vbl_name] = False
        else:
          # Throw an exception
          print(vbl_format + ' value dictionary not found.')
      else:
        record_dict[vbl_name] = value
        if (vbl_label in self.variable_type and
            self.variable_type[vbl_label] == "bool"):
          record_dict[vbl_name] = False
          
      if not vbl_name in record_dict or record_dict[vbl_name] == "":
        if self.variable_type[vbl_label] == "int32":
          record_dict[vbl_name] = NULL_INT_VALUE
        elif self.variable_type[vbl_label] == "float32":
          record_dict[vbl_name] = NULL_FLOAT_VALUE
          
    return record_dict


# Intended for cleaning column names.
def clean_name(vbl_name):
  name = re.sub('\(|\)', ' ', vbl_name)
  name = re.sub(':', '-', name)
  name = re.sub('\'|\"', "", name)
  return name


def main():
  parser = optparse.OptionParser(usage='%prog data_dir')
  opts, args = parser.parse_args()
  if len(args) < 1:
    parser.error('Please specify a data directory.')
  elif len(args) > 1:
    parser.error('Too many arguments.')
  
  aws_ip = input("IP Address of the AWS instance:")
  pg_username = input("Please enter Postgres username:")
  pg_password = input("Password:")
    
  pg_login = pg_username + ":" + pg_password
  pg_conn_str = 'postgresql://' + pg_login + '@' + aws_ip + ':5432/dhs_data'
  #engine = create_engine(pg_conn_str, echo=False, paramstyle='format')

  vbl_pattern = "attrib (?P<label>\S+)\s+(?:length=(?P<length>\$?\d+))?"
  vbl_pattern += "\s*(?:format=(?P<format>\S*)\.)?"
  vbl_pattern += "\s*(?:label=\"(?P<desc>.*)\")?;"
  bytewise_pattern = "@(\d+)\s*(\S+)\s*(\$?\d+\.?\d*)*"
  nullrule_pattern = "if (\S+)\s+=\s+(\S+) then \1 = (.*);"
  
  print("Path = " + args[0])
  print(args[0] + '/*.zip')
  print(glob.glob(args[0] + '/*'))
  
  index_cols = dict()
  
  for zfile in sorted(glob.glob(args[0] + '/*.zip')):
    print("Zipfile = " + zfile)
    base_filename = re.search('/?(\w*)\.zip', zfile, re.IGNORECASE).group(1)
    tmpdir = tempfile.mkdtemp(prefix='dhs_zip-')
    with zipfile.ZipFile(zfile, mode="r") as zf_fh:
      schemafiles = set()
      datafiles = set()
      for fname in zf_fh.namelist():
        if re.search('\.SAS', fname, re.IGNORECASE): schemafiles.add(fname)
        if re.search('\.DAT', fname, re.IGNORECASE): datafiles.add(fname)
        # Add in functionality for CHLDLINE data where appropriate.
        # Add in support for multiple .DAT and .SAS files in one .ZIP
        #   where appropriate.
      print("Schemas = " + str(schemafiles) + ", data = " + str(datafiles))
      if len(schemafiles) == 0:
        print('Missing schema in zipfile ' + base_filename + '.ZIP')
        continue
      if len(datafiles) == 0:
        print('Missing datafile in zipfile ' + base_filename + '.ZIP')
        continue
      print("Tmpdir = " + tmpdir)
      for schemafile in schemafiles:
        zf_fh.extract(schemafile, tmpdir)
      for datafile in datafiles:
        zf_fh.extract(datafile, tmpdir)

    country_code = base_filename[0:2]
    survey_type = base_filename[2:4]
    survey_version = base_filename[4:6]
    base_table_name = "DHS_" + COUNTRY_CODES[country_code]
    if COUNTRY_CODES[country_code] == "India":
      base_table_name += "-" + INDIA_STATE_CODES[country_code]
    if survey_type in DATASET_CODES:
      base_table_name += "-" + DATASET_CODES[survey_type]
    else:
      base_table_name += "-" + survey_type + " Form"
    base_table_name += "-v" + survey_version
    table_cnt = 0
    writes_succeeded = True
    for schemafile in schemafiles:
      fname = schemafile.split('.')[-2]
      datafile = fname + ".DAT"
      if not datafile in datafiles:
        datafile = fname + ".dat"
        if not datafile in datafiles:
          continue
    
      index_cols = set()               # Keeps a list of index variables
      data_dict = DataDictionary(base_filename)
    
      in_value_dict_defn = False
      vbl_format = ""
      is_string_value = False
    
      # We are using the .SAS file as a schema, even though it is a perfectly
      # good SAS program in its own right.  This way 1) I don't have to learn
      # SAS, and 2) I don't need to get a SAS license.
      with open(os.path.join(tmpdir, schemafile), mode="r",
                encoding="Latin-1") as sf:
        for line in sf: 
          # First try to parse the line as a mapping from variable label to
          # a string describing the meaning of the variable.  As the latter can
          # be duplicated (and since we do not want duplicate column names), we
          # append an incremented number to duplicate names.
          # Additionally, the line may contain information on the length of
          # the encoded field, or the format (data dictionary) we should use to
          # interpret the values the field takes.
          # Example: "  attrib Q834Y_2  label="Year on guideline(2)";"
          # Example: "  attrib SDOMAIN  length=4;"
          # Example: "  attrib UTYPE    format=F00001_. label="unit type";"
          vbl_match = re.search(vbl_pattern, line)
        
          # Next case is we are in the byte-wise definition of the flat file
          # records.  Example: "@164  Q831     1.0"
          bytewise_match = re.search(bytewise_pattern, line)
          
          # Next case is we are in a replacement rule for dealing with null
          # values.  Example: "if Q805     =      9 then Q805 = .;"
          nullrule_match = re.search(nullrule_pattern, line)
          
          # If _that_ fails, we may be in a sub-dictionary mapping encoded
          # values to display values.  Example:
          # "  value F00028_
          #      1 = "Yes"
          #      2 = "No"
          #      ;                "
          value_start_match = re.search("value (\S+)\s*", line)
        
          # N.B. The ordering in the .SAS file is actually value-mapping 
          # (value_start_match), followed by label-name matching (vbl_match),
          # followed by the bytewise breakdown (bytewise match), followed by
          # null rules (nullrule_match).  There are also a small number of 
          # lines that do not fit any of these patterns.
        
          if vbl_match:
            vbl_label = vbl_match.group("label")
            vbl_name = vbl_label
            if vbl_match.group("desc"):
              vbl_name += ' ' + vbl_match.group("desc")
            vbl_name = clean_name(vbl_name)
            data_dict.vbls_seen.add(vbl_name)
            data_dict.variable_dict[vbl_label] = vbl_name
            for idx_col_pattern in INDEX_COLUMNS:
              if re.search(idx_col_pattern, vbl_name, re.IGNORECASE):
                index_cols.add(vbl_name)
            if vbl_match.group("format"):
              data_dict.variable_format_dict[vbl_label] = vbl_match.group(
                  "format")
          elif bytewise_match:
            start_pos = int(bytewise_match.group(1))
            vbl_label = bytewise_match.group(2)
            num_len_string = bytewise_match.group(3)
            data_dict.add_bytewise_encoding(start_pos, vbl_label,
                                            num_len_string)
            if re.search('\$\d+\.', line):
              data_dict.key_type[vbl_label] = "string"
          elif nullrule_match:
            vbl_label = nullrule_match.group(1)
            null_value = nullrule_match.group(2)
            data_dict.add_null_rule(vbl_label, null_value)
          elif value_start_match:
            vbl_format = value_start_match.group(1)
#            print("Starting value dict for format |" + value_code)
            in_value_dict_defn = True
            data_dict.value_dict[vbl_format] = dict()
            if re.search("\$\w*_", vbl_format):
              is_string_value = True
            else:
              is_string_value = False
          elif in_value_dict_defn:
#            print("In value dict for format |" + vbl_format + "| " + line)
            vmap_match = None
            if re.search("\s*;\s*", line):
              in_value_dict_defn = False
            elif is_string_value:
              vmap_match = re.search(
                  "(?P<quotea>[\"\'])(?P<value>\S*)\s*(?P=quotea) = " +
                  "(?P<quoteb>[\"\'])(?P<display>.*)(?P=quoteb)",
                  line)
            else:
              vmap_match = re.search("(?P<value>\d*\.?\d*) = (?P<quote>[\"\'])"
                                     + "(?P<display>.*)(?P=quote)",
                                     line)
            if vmap_match:
              value = vmap_match.group("value")
              if not is_string_value:
                if re.search("\.", value):
                  value = float(value)
                else: value = int(value)
                data_dict.value_dict[vbl_format][value] = vmap_match.group(
                    "display")
#        sys.stdout.flush()
          
      print("Schema read, " + str(len(data_dict.vbls_seen)) +
            " variables seen.")
      
      data_dict.clean_formats()

      # Now we've read off the schema describing how to parse the flat file
      # records into dataframe records.  Now we just need to do the parsing.
#      df = pandas.DataFrame(columns=data_dict.vbls_seen)
      record_cnt = 0
      data_records = []
      with open(os.path.join(tmpdir, datafile), mode="r") as data:
        for record in data:
#         record = record.decode('utf-8')
#         print("Length of record = " + str(len(record)))
          record_cnt += 1
          # For testing
          if RECORD_LIMIT > 0 and record_cnt > RECORD_LIMIT: break
          if record_cnt % 5000 == 0:
            print ("Read " + str(record_cnt) + " records.")
          record_dict = data_dict.parse(record)
#         print("Record has " + str(len(record_dict)) + " entries.")
          data_records.append(record_dict)
#        df = df.append(data_records, ignore_index=True)
      
      print("Data file read; " + str(record_cnt) + " records seen.")

      if record_cnt == 0:
        print("No records found; misread file?")
        writes_succeeded = False

      try:
        col_cnt = 0
        col_set = set()
        col_set |= index_cols
        columns = list(data_dict.vbls_seen)
        while col_cnt < len(columns):
          col_set.add(columns[col_cnt])
          col_cnt += 1
          if (col_cnt % MAX_COL_CNT) == 0 or col_cnt == len(columns):
            table_name = base_table_name
            if table_cnt > 0:
              table_name += "-" + str(table_cnt)
            print("Writing to " + table_name)
#            export_df = df.loc[:, list(col_set)]
#            dshape = export_df.shape
            #print(export_df.shape)
            #export_df.to_sql(name=table_name, con=engine, if_exists='replace')
            try:
              drop(pg_conn_str + "::" + table_name)
            except:
              print(table_name + " does not exist.  Creating from scratch.")
            #table_csv = os.path.join(tmpdir, table_name + ".csv")
            #print("Writing temp CSV to " + table_csv)
#            export_df.to_csv(table_csv)
            #with open(table_csv, "w") as tfh:
            col_headers = list(col_set)
            #  writer = csv.DictWriter(tfh, col_headers)
            #  writer.writeheader()
            temp_table = []
#            header_str = ""
#            type_str = ""
            dshape_str = "var * {"
            for k in col_headers:
#              print("|" + k.split(" ")[0] + "|")
#              print(data_dict.variable_type[k.split(" ")[0]])
              #header_str += "\'" + k + "\', "
              dshape_str += "\"" + k + "\": "
              if k.split(" ")[0] in data_dict.variable_type:
                #type_str += data_dict.variable_type[k] + ", "
                dshape_str += data_dict.variable_type[k.split(" ")[0]] + ", "
              else:
                dshape_str += "int32, "
                #type_str += "int32, "
#             dshape_str += "\"" + k + "\": " + data_dict.variable_type[k] + ","
            #dshape_str = "var * struct[[" + header_str[:-2] + "],["
            #dshape_str += type_str[:-2] + "]]"
            dshape_str = dshape_str[:-2] + "}"
            for row in data_records:
              temp_row = {k: row[k] for k in col_headers if k in row}
              temp_row.update({k: "" for k in col_headers if (
                  k.split(" ")[0] in data_dict.variable_type and
                  data_dict.variable_type[k.split(" ")[0]] == "string" and
                  not k in row)})
              temp_row.update({k: NULL_INT_VALUE for k in col_headers if (
                  k.split(" ")[0] in data_dict.variable_type and
                  data_dict.variable_type[k.split(" ")[0]] == "int32" and
                  not k in row)})
              temp_row.update({k: NULL_FLOAT_VALUE for k in col_headers if (
                  k.split(" ")[0] in data_dict.variable_type and
                  data_dict.variable_type[k.split(" ")[0]] == "float32" and
                  not k in row)})
              temp_row.update({k: False for k in col_headers if (
                  k.split(" ")[0] in data_dict.variable_type and
                  data_dict.variable_type[k.split(" ")[0]] == "bool" and
                  not k in row)})
              temp_row.update({k: NULL_INT_VALUE for k in col_headers if not (
                  k.split(" ")[0] in data_dict.variable_type or k in row)})
              temp_table.append(temp_row)
          #      writer.writerow(temp_row)
            print("Wrote temp CSV")
            start_time = timeit.default_timer()
          #  odo(table_csv, pg_conn_str + "::" + table_name)
            odo(temp_table, pg_conn_str + "::" + table_name,
                dshape = dshape_str)
            elapsed = timeit.default_timer() - start_time
            col_set.clear()
            col_set |= index_cols
            table_cnt += 1
            print("Finished writing to " + table_name + " in " +
                  str(elapsed) + "s")
      except:
        writes_succeeded = False
        print("Could not write tables for " + schemafile + " in " + zfile)
        
    shutil.rmtree(tmpdir)
    if writes_succeeded:
      os.remove(zfile)

#    df.to_sql(name=table_name, con=engine, if_exists='replace')
#    print(table_name)
#    print(df)
    
    
if __name__ == '__main__':
  main()